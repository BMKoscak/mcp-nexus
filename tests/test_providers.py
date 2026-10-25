"""Provider adapters against canned vendor streams (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from nexus.providers import (
    AnthropicProvider,
    Done,
    Message,
    OllamaProvider,
    OpenAIProvider,
    ProviderError,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolDef,
)
from nexus.providers.anthropic import to_anthropic_messages

TOOLS = [ToolDef("system_stats", "Stats", {"type": "object", "properties": {}})]
HISTORY = [
    Message("user", "how busy is my pc?"),
    Message("assistant", "Checking.", tool_calls=[ToolCall("t1", "system_stats", {})]),
    Message("tool", '{"cpu": 12}', tool_call_id="t1", name="system_stats"),
]


def sse_body(events: list[dict], *, with_event_lines: bool = False, done: bool = False) -> bytes:
    out = []
    for e in events:
        if with_event_lines:
            out.append(f"event: {e['type']}")
        out.append(f"data: {json.dumps(e)}")
        out.append("")
    if done:
        out += ["data: [DONE]", ""]
    return ("\n".join(out) + "\n").encode()


async def collect(provider, model="m", messages=HISTORY, tools=TOOLS):
    return [ev async for ev in provider.stream(model, messages, tools, system="be brief")]


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── Anthropic ────────────────────────────────────────────────
async def test_anthropic_stream_text_and_tool_use():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["headers"] = req.headers
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hi"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "web_fetch",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"url": "https:'},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '//x.io"}'},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 9},
            },
            {"type": "message_stop"},
        ]
        return httpx.Response(200, content=sse_body(events, with_event_lines=True))

    async with client(handler) as http:
        events = await collect(AnthropicProvider(http, api_key="sk-ant", base_url=None), "claude-x")

    assert events[0] == TextDelta("Hi")
    assert events[1] == ToolCallEvent(ToolCall("tu_1", "web_fetch", {"url": "https://x.io"}))
    assert events[2] == Done("tool_use", {"input_tokens": 20, "output_tokens": 9})
    body = seen["body"]
    assert seen["headers"]["x-api-key"] == "sk-ant" and seen["headers"]["anthropic-version"]
    assert body["system"] == "be brief" and body["tools"][0]["input_schema"]["type"] == "object"
    # tool result became a user turn with a tool_result block, after the assistant tool_use
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"][1] == {
        "type": "tool_use",
        "id": "t1",
        "name": "system_stats",
        "input": {},
    }
    assert body["messages"][2]["content"][0]["type"] == "tool_result"


def test_anthropic_merges_parallel_tool_results():
    msgs = [
        Message("user", "go"),
        Message("assistant", "", tool_calls=[ToolCall("a", "x", {}), ToolCall("b", "y", {})]),
        Message("tool", "1", tool_call_id="a"),
        Message("tool", "2", tool_call_id="b", is_error=True),
    ]
    out = to_anthropic_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["a", "b"]
    assert out[2]["content"][1]["is_error"] is True


async def test_anthropic_requires_key_and_surfaces_http_errors():
    async with client(
        lambda r: httpx.Response(401, json={"error": {"message": "invalid x-api-key"}})
    ) as http:
        with pytest.raises(ProviderError, match="No API key"):
            await collect(AnthropicProvider(http, api_key=None, base_url=None))
        with pytest.raises(ProviderError, match="401.*invalid x-api-key"):
            await collect(AnthropicProvider(http, api_key="bad", base_url=None))


# ── OpenAI ───────────────────────────────────────────────────
async def test_openai_stream_assembles_tool_call_fragments():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        chunks = [
            {"choices": [{"delta": {"content": "Let me"}}]},
            {"choices": [{"delta": {"content": " check."}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_9",
                                    "function": {"name": "system_stats", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}}
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
        ]
        return httpx.Response(200, content=sse_body(chunks, done=True))

    async with client(handler) as http:
        events = await collect(OpenAIProvider(http, api_key="sk-o", base_url=None), "gpt-x")

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Let me", " check."]
    assert ToolCallEvent(ToolCall("call_9", "system_stats", {})) in events
    assert events[-1] == Done("tool_calls", {"input_tokens": 5, "output_tokens": 7})
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    msgs = seen["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "be brief"}
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "system_stats", "arguments": "{}"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "t1", "content": '{"cpu": 12}'}


async def test_openai_compatible_server_needs_no_key():
    def handler(req: httpx.Request) -> httpx.Response:
        assert "authorization" not in req.headers
        return httpx.Response(200, json={"data": [{"id": "qwen"}, {"id": "llama"}]})

    async with client(handler) as http:
        p = OpenAIProvider(http, api_key=None, base_url="http://localhost:1234/v1")
        assert [m.id for m in await p.list_models()] == ["llama", "qwen"]


# ── Ollama ───────────────────────────────────────────────────
async def test_ollama_ndjson_stream_with_tools():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        lines = [
            {"message": {"role": "assistant", "content": "On it"}, "done": False},
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "system_stats", "arguments": {"x": 1}}}],
                },
                "done": False,
            },
            {
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 3,
                "eval_count": 4,
            },
        ]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())

    async with client(handler) as http:
        events = await collect(OllamaProvider(http, api_key=None, base_url=None), "llama3.2")

    assert events[0] == TextDelta("On it")
    assert isinstance(events[1], ToolCallEvent) and events[1].call.arguments == {"x": 1}
    assert events[-1] == Done("stop", {"input_tokens": 3, "output_tokens": 4})
    msgs = seen["body"]["messages"]
    assert msgs[3] == {"role": "tool", "content": '{"cpu": 12}', "tool_name": "system_stats"}
    assert seen["body"]["tools"][0]["function"]["name"] == "system_stats"


async def test_ollama_error_line_raises():
    async with client(
        lambda r: httpx.Response(200, content=b'{"error": "model not found"}\n')
    ) as http:
        with pytest.raises(ProviderError, match="model not found"):
            await collect(OllamaProvider(http, api_key=None, base_url=None))


# ── Gemini ───────────────────────────────────────────────────
from nexus.providers import GeminiProvider  # noqa: E402
from nexus.providers.gemini import clean_schema, to_function_declaration, to_gemini_contents  # noqa: E402


async def test_gemini_stream_text_function_call_and_signature():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["headers"] = req.headers
        seen["body"] = json.loads(req.content)
        chunks = [
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "Thinking about it", "thought": True}],
                        }
                    }
                ]
            },
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Let me "}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "check."}]}}]},
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "web_fetch",
                                        "args": {"url": "https://x.io"},
                                    },
                                    "thoughtSignature": "SIG_A",
                                },
                                {"functionCall": {"name": "system_stats", "args": {}}},
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 11,
                    "candidatesTokenCount": 6,
                    "totalTokenCount": 17,
                },
            },
        ]
        return httpx.Response(200, content=sse_body(chunks))

    async with client(handler) as http:
        events = await collect(GeminiProvider(http, api_key="AIza-test", base_url=None), "gemini-x")

    assert [e.text for e in events if isinstance(e, TextDelta)] == [
        "Let me ",
        "check.",
    ]  # thought skipped
    calls = [e.call for e in events if isinstance(e, ToolCallEvent)]
    assert [c.name for c in calls] == ["web_fetch", "system_stats"]
    assert calls[0].arguments == {"url": "https://x.io"}
    assert calls[0].extra == {"thoughtSignature": "SIG_A"} and calls[1].extra == {}
    assert events[-1] == Done("STOP", {"input_tokens": 11, "output_tokens": 6})

    assert (
        seen["url"]
        == "https://generativelanguage.googleapis.com/v1beta/models/gemini-x:streamGenerateContent?alt=sse"
    )
    assert seen["headers"]["x-goog-api-key"] == "AIza-test"
    body = seen["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    decl = body["tools"][0]["functionDeclarations"][0]
    assert (
        decl["name"] == "system_stats" and "parametersJsonSchema" not in decl
    )  # no params → key omitted
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["contents"][2]["parts"][0]["functionResponse"] == {
        "name": "system_stats",
        "response": {"result": '{"cpu": 12}'},
    }


def test_gemini_replays_signatures_and_keeps_parallel_order():
    calls = [
        ToolCall("fc1", "a", {"x": 1}, {"thoughtSignature": "SIG", "gemini_id": "fc1"}),
        ToolCall("call_local", "b", {}),
    ]
    contents = to_gemini_contents(
        [
            Message("user", "go"),
            Message("assistant", "", tool_calls=calls),
            Message("tool", "r1", tool_call_id="fc1", name="a"),
            Message("tool", "boom", tool_call_id="call_local", name="b", is_error=True),
            Message("assistant", "done"),
        ]
    )
    assert [c["role"] for c in contents] == ["user", "model", "user", "model"]
    model_parts = contents[1]["parts"]
    assert model_parts[0] == {
        "functionCall": {"name": "a", "args": {"x": 1}, "id": "fc1"},
        "thoughtSignature": "SIG",
    }
    assert model_parts[1] == {
        "functionCall": {"name": "b", "args": {}}
    }  # unsigned call sent unchanged, no invented id
    # FC1, FC2, then FR1, FR2 — never interleaved
    responses = [p["functionResponse"] for p in contents[2]["parts"]]
    assert responses == [
        {"name": "a", "response": {"result": "r1"}, "id": "fc1"},
        {"name": "b", "response": {"error": "boom"}},
    ]


def test_gemini_schema_cleaning_inlines_refs():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "mode": {"$ref": "#/$defs/Mode", "description": "How"},
            "n": {"type": ["integer", "null"]},
        },
        "required": ["mode"],
        "$defs": {"Mode": {"enum": ["create", "append"], "type": "string"}},
    }
    cleaned = clean_schema(schema)
    assert "$defs" not in cleaned and "$schema" not in cleaned
    assert cleaned["properties"]["mode"] == {
        "enum": ["create", "append"],
        "type": "string",
        "description": "How",
    }
    assert cleaned["properties"]["n"] == {"type": ["integer", "null"]}  # JSON Schema kept as-is
    assert schema["$defs"]  # input not mutated
    decl = to_function_declaration(ToolDef("t", "d", schema))
    assert decl["parametersJsonSchema"]["required"] == ["mode"]


async def test_gemini_list_models_filters_and_paginates():
    pages = {
        None: {
            "models": [
                {
                    "name": "models/gemini-a",
                    "displayName": "Gemini A",
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                },
                {"name": "models/text-embedding-x", "supportedGenerationMethods": ["embedContent"]},
            ],
            "nextPageToken": "p2",
        },
        "p2": {
            "models": [
                {"name": "models/gemini-b", "supportedGenerationMethods": ["generateContent"]}
            ]
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[req.url.params.get("pageToken")])

    async with client(handler) as http:
        models = await GeminiProvider(http, api_key="k", base_url=None).list_models()
    assert [(m.id, m.name) for m in models] == [("gemini-a", "Gemini A"), ("gemini-b", "gemini-b")]


async def test_gemini_errors_surface_message():
    body = {
        "error": {
            "code": 400,
            "message": "Function call is missing a thought_signature",
            "status": "INVALID_ARGUMENT",
        }
    }
    async with client(lambda r: httpx.Response(400, json=body)) as http:
        with pytest.raises(ProviderError, match="400.*thought_signature"):
            await collect(GeminiProvider(http, api_key="k", base_url=None))
        with pytest.raises(ProviderError, match="No API key"):
            await collect(GeminiProvider(http, api_key=None, base_url=None))
