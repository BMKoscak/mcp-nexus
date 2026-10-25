"""Command line entry point.

nexus serve [--host H] [--port P]   web UI + REST API + MCP endpoint on /mcp
nexus stdio                         MCP over stdio (for Claude Desktop & co.)
nexus tools                         print the tools the connectors expose
nexus --project-dir DIR ...         run as if started from DIR (.env, data/, workspace/)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from nexus import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nexus", description="MCP Nexus — local MCP hub")
    parser.add_argument("--version", action="version", version=f"mcp-nexus {__version__}")
    parser.add_argument(
        "--project-dir",
        help="Directory holding .env, data/, workspace/ and plugins/ (default: current directory)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run the web UI, REST API and /mcp endpoint")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    sub.add_parser("stdio", help="Serve MCP over stdin/stdout")
    sub.add_parser("tools", help="List the tools exposed by enabled connectors")
    args = parser.parse_args(argv)

    if args.project_dir:
        os.chdir(args.project_dir)

    from nexus.config import Settings

    overrides = {}
    if getattr(args, "host", None):
        overrides["host"] = args.host
    if getattr(args, "port", None):
        overrides["port"] = args.port
    settings = Settings(**overrides)

    if args.command == "serve":
        _serve(settings)
    elif args.command == "stdio":
        asyncio.run(_stdio(settings))
    elif args.command == "tools":
        asyncio.run(_tools(settings))


def _serve(settings) -> None:
    import uvicorn

    from nexus.main import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


async def _stdio(settings) -> None:
    # stdout carries the protocol: every log line must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    from nexus.mcp_engine import MCPEngine
    from nexus.templates import TemplateManager

    engine = MCPEngine(settings)
    engine.mode = "stdio"
    await engine.start(host_client=False)
    try:
        await TemplateManager(engine, engine.db).load_all()
        await engine.server.run_stdio_async()
    finally:
        await engine.stop()


async def _tools(settings) -> None:
    logging.basicConfig(level=logging.WARNING)
    from nexus.mcp_engine import MCPEngine

    async with MCPEngine(settings) as engine:
        for rec in engine.registry.public():
            print(f"[{rec['status']:>8}] {rec['name']}  ({rec['source']})")
            for tool in rec["tools"]:
                flag = "  ⚠ approval" if tool["requires_approval"] else ""
                print(f"           - {tool['name']}{flag}")
            if rec["error"]:
                print("           " + rec["error"].strip().splitlines()[-1])


if __name__ == "__main__":  # pragma: no cover
    main()
