"""Fetch web pages and turn them into model-friendly text.

Safety: only http(s); every hop of a redirect chain is re-checked by the SSRF
guard (no loopback/private targets unless disabled); responses are streamed
and cut off at ``max_bytes``.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from nexus.connectors.base import Connector, ToolError, tool
from nexus.netguard import BlockedAddressError, check_url

DROP_TAGS = ["script", "style", "noscript", "svg", "iframe", "template", "canvas", "form"]
BLOCK_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "li",
    "pre",
    "blockquote",
    "td",
    "th",
    "dt",
    "dd",
}
REDIRECT_CODES = {301, 302, 303, 307, 308}


class WebFetchConfig(BaseModel):
    block_private: bool | None = Field(None, description="Override NEXUS_FETCH_BLOCK_PRIVATE.")
    max_bytes: int = Field(0, ge=0, description="Override NEXUS_FETCH_MAX_BYTES when non-zero.")
    max_redirects: int = Field(5, ge=0, le=20)
    default_max_chars: int = Field(20_000, ge=500, description="Default cap on returned text.")


class FetchResult(BaseModel):
    url: str
    status: int
    content_type: str
    body: bytes
    truncated: bool


class WebFetchConnector(Connector):
    name = "web"
    description = "Fetch web pages as clean text or markdown, extract links, read JSON APIs."
    Config = WebFetchConfig

    config: WebFetchConfig

    # ── guarded GET ──────────────────────────────────────────
    async def _get(self, url: str, accept: str = "*/*") -> FetchResult:
        s = self.ctx.settings
        block = (
            s.fetch_block_private
            if self.config.block_private is None
            else self.config.block_private
        )
        limit = self.config.max_bytes or s.fetch_max_bytes
        current = url
        for _hop in range(self.config.max_redirects + 1):
            try:
                await check_url(current, block_private=block)
            except BlockedAddressError as exc:
                raise ToolError(str(exc)) from None
            try:
                async with self.ctx.http.stream(
                    "GET", current, headers={"Accept": accept}, follow_redirects=False
                ) as resp:
                    if resp.status_code in REDIRECT_CODES and "location" in resp.headers:
                        current = urljoin(current, resp.headers["location"])
                        continue
                    chunks: list[bytes] = []
                    size = 0
                    truncated = False
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= limit:
                            truncated = True
                            break
                    body = b"".join(chunks)[:limit]
                    return FetchResult(
                        url=str(resp.url),
                        status=resp.status_code,
                        content_type=resp.headers.get("content-type", ""),
                        body=body,
                        truncated=truncated,
                    )
            except httpx.HTTPError as exc:
                raise ToolError(f"Fetch failed: {exc.__class__.__name__}: {exc}") from None
        raise ToolError(f"Too many redirects (>{self.config.max_redirects}).")

    # ── tools ────────────────────────────────────────────────
    @tool(read_only=True, open_world=True)
    async def fetch(
        self,
        url: str,
        format: Annotated[
            Literal["markdown", "text", "html"],
            Field(description="markdown keeps headings/lists; text is plain; html is raw"),
        ] = "markdown",
        max_chars: Annotated[int | None, Field(description="Cap on returned characters")] = None,
    ) -> dict[str, Any]:
        """Fetch a URL and return its title and readable content."""
        res = await self._get(
            url, accept="text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"
        )
        text = _decode(res)
        title = ""
        if "html" in res.content_type or text.lstrip()[:15].lower().startswith(
            ("<!doctype", "<html")
        ):
            tree = HTMLParser(text)
            title = (tree.css_first("title").text(strip=True) if tree.css_first("title") else "")[
                :300
            ]
            if format == "markdown":
                text = html_to_markdown(tree)
            elif format == "text":
                text = html_to_text(tree)
        cap = max_chars or self.config.default_max_chars
        clipped = len(text) > cap
        return {
            "url": res.url,
            "status": res.status,
            "content_type": res.content_type,
            "title": title,
            "content": text[:cap],
            "truncated": clipped or res.truncated,
        }

    @tool(read_only=True, open_world=True)
    async def extract_links(
        self,
        url: str,
        same_domain_only: bool = False,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict]:
        """List the links on a page (absolute URL + anchor text)."""
        res = await self._get(url, accept="text/html")
        tree = HTMLParser(_decode(res))
        base_host = urlsplit(res.url).hostname
        seen: set[str] = set()
        links: list[dict] = []
        for a in tree.css("a[href]"):
            href = urljoin(res.url, a.attributes.get("href") or "")
            if not href.startswith(("http://", "https://")) or href in seen:
                continue
            if same_domain_only and urlsplit(href).hostname != base_host:
                continue
            seen.add(href)
            links.append({"url": href, "text": a.text(strip=True)[:200]})
            if len(links) >= limit:
                break
        return links

    @tool(read_only=True, open_world=True)
    async def fetch_json(self, url: str) -> Any:
        """GET a JSON API endpoint and return the parsed result."""
        res = await self._get(url, accept="application/json")
        if res.truncated:
            raise ToolError("Response exceeded the size limit; cannot parse partial JSON.")
        try:
            return json.loads(_decode(res))
        except json.JSONDecodeError as exc:
            raise ToolError(f"Response is not valid JSON ({exc.msg}).") from None


# ── HTML conversion ──────────────────────────────────────────
def _decode(res: FetchResult) -> str:
    match = re.search(r"charset=([\w-]+)", res.content_type, re.I)
    encoding = match.group(1) if match else "utf-8"
    try:
        return res.body.decode(encoding, errors="replace")
    except LookupError:
        return res.body.decode("utf-8", errors="replace")


def _clean(tree: HTMLParser) -> Node | None:
    tree.strip_tags(DROP_TAGS)
    return tree.body or tree.root


def html_to_text(tree: HTMLParser) -> str:
    root = _clean(tree)
    if root is None:
        return ""
    return _squash(root.text(separator="\n"))


def html_to_markdown(tree: HTMLParser) -> str:
    root = _clean(tree)
    if root is None:
        return ""
    lines: list[str] = []
    for node in root.traverse():
        tag = node.tag
        if tag not in BLOCK_TAGS or _has_block_ancestor(node):
            continue
        if tag == "pre":
            lines.append("```\n" + (node.text() or "").strip("\n") + "\n```")
            continue
        text = " ".join((node.text(separator="") or "").split())
        if not text:
            continue
        if tag[0] == "h" and tag[1:].isdigit():
            lines.append("#" * int(tag[1:]) + " " + text)
        elif tag == "li":
            lines.append("- " + text)
        elif tag == "blockquote":
            lines.append("> " + text)
        else:
            lines.append(text)
    if not lines:  # pages built from divs only
        return html_to_text(tree)
    return "\n\n".join(lines)


def _has_block_ancestor(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag in BLOCK_TAGS:
            return True
        parent = parent.parent
    return False


def _squash(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()
