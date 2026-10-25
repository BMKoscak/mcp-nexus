"""Sandboxed filesystem access rooted at ``NEXUS_WORKSPACE_DIR``.

Every path is resolved (following symlinks) and must stay inside the root, so
``../`` tricks, absolute paths elsewhere and symlink escapes are all rejected.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from nexus.connectors.base import Connector, ToolError, resource, tool

MAX_LIST_ENTRIES = 1000


class FilesystemConfig(BaseModel):
    root: str = Field("", description="Sandbox root. Empty = NEXUS_WORKSPACE_DIR.")
    allow_write: bool = Field(True, description="Allow creating and modifying files.")
    max_read_bytes: int = Field(1_000_000, ge=1, description="Largest file read_file will return.")
    max_write_bytes: int = Field(5_000_000, ge=1, description="Largest content write_file accepts.")


class FilesystemConnector(Connector):
    name = "filesystem"
    description = "Read, write, list and search files inside the sandboxed workspace folder."
    Config = FilesystemConfig

    config: FilesystemConfig

    async def startup(self) -> None:
        root = Path(self.config.root) if self.config.root else self.ctx.settings.workspace_dir
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()

    # ── helpers ──────────────────────────────────────────────
    def _resolve(self, path: str) -> Path:
        if "\x00" in path:
            raise ToolError("Invalid path.")
        candidate = Path(path)
        target = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        if target != self.root and not target.is_relative_to(self.root):
            raise ToolError(f"Path {path!r} is outside the workspace.")
        return target

    def _rel(self, p: Path) -> str:
        rel = p.relative_to(self.root).as_posix()
        return rel or "."

    def _require_write(self) -> None:
        if not self.config.allow_write:
            raise ToolError("Writing is disabled for the filesystem connector.")

    # ── tools ────────────────────────────────────────────────
    @tool(read_only=True)
    async def list_dir(
        self,
        path: Annotated[str, Field(description="Directory relative to the workspace root")] = ".",
        recursive: bool = False,
    ) -> list[dict]:
        """List files and folders. Returns name, type, size and modification time."""
        target = self._resolve(path)
        if not target.is_dir():
            raise ToolError(f"{path!r} is not a directory.")

        def walk() -> list[dict]:
            entries = []
            iterator = target.rglob("*") if recursive else target.iterdir()
            for p in sorted(iterator):
                if len(entries) >= MAX_LIST_ENTRIES:
                    break
                try:
                    st = p.stat()
                except OSError:
                    continue
                entries.append(
                    {
                        "path": self._rel(p),
                        "type": "dir" if p.is_dir() else "file",
                        "size": None if p.is_dir() else st.st_size,
                        "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(
                            timespec="seconds"
                        ),
                    }
                )
            return entries

        return await asyncio.to_thread(walk)

    @tool(read_only=True)
    async def read_file(
        self,
        path: Annotated[str, Field(description="File path relative to the workspace root")],
        encoding: str = "utf-8",
    ) -> str:
        """Read a text file from the workspace."""
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"File {path!r} not found.")
        size = target.stat().st_size
        if size > self.config.max_read_bytes:
            raise ToolError(f"File is {size} bytes; limit is {self.config.max_read_bytes}.")
        try:
            return await asyncio.to_thread(target.read_text, encoding)
        except UnicodeDecodeError:
            raise ToolError(f"{path!r} is not valid {encoding} text (binary file?).") from None

    @tool(destructive=True)
    async def write_file(
        self,
        path: Annotated[str, Field(description="File path relative to the workspace root")],
        content: str,
        mode: Annotated[
            Literal["create", "overwrite", "append"],
            Field(description="create fails if the file exists"),
        ] = "create",
    ) -> str:
        """Write text to a file in the workspace, creating parent folders as needed."""
        self._require_write()
        if len(content.encode()) > self.config.max_write_bytes:
            raise ToolError(f"Content exceeds {self.config.max_write_bytes} bytes.")
        target = self._resolve(path)
        if target.is_dir():
            raise ToolError(f"{path!r} is a directory.")
        if mode == "create" and target.exists():
            raise ToolError(f"{path!r} already exists. Use mode='overwrite' or 'append'.")

        def write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a" if mode == "append" else "w", encoding="utf-8", newline="") as f:
                f.write(content)

        await asyncio.to_thread(write)
        return f"Wrote {len(content)} characters to {self._rel(target)} ({mode})."

    @tool()
    async def make_dir(self, path: str) -> str:
        """Create a folder (and any missing parents) in the workspace."""
        self._require_write()
        target = self._resolve(path)
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
        return f"Created {self._rel(target)}/"

    @tool(read_only=True)
    async def file_info(self, path: str) -> dict[str, Any]:
        """Size, type and timestamps for a path."""
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"{path!r} does not exist.")
        st = target.stat()
        return {
            "path": self._rel(target),
            "type": "dir" if target.is_dir() else "file",
            "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds"),
            "created": datetime.fromtimestamp(st.st_ctime, UTC).isoformat(timespec="seconds"),
        }

    @tool(read_only=True)
    async def search(
        self,
        pattern: Annotated[str, Field(description="Glob on file names, e.g. '*.md'")] = "*",
        contains: Annotated[
            str | None, Field(description="Only files containing this text")
        ] = None,
        path: str = ".",
        max_results: int = 100,
    ) -> list[str]:
        """Find files by name pattern and, optionally, by text content."""
        base = self._resolve(path)
        limit = max(1, min(max_results, MAX_LIST_ENTRIES))

        def run() -> list[str]:
            hits: list[str] = []
            for dirpath, _dirs, files in os.walk(base):
                for fname in files:
                    if not fnmatch.fnmatch(fname, pattern):
                        continue
                    p = Path(dirpath) / fname
                    if contains is not None:
                        try:
                            if p.stat().st_size > self.config.max_read_bytes:
                                continue
                            if contains not in p.read_text("utf-8", errors="ignore"):
                                continue
                        except OSError:
                            continue
                    hits.append(self._rel(p))
                    if len(hits) >= limit:
                        return hits
            return hits

        return await asyncio.to_thread(run)

    # ── resources ────────────────────────────────────────────
    @resource("nexus://filesystem/tree", name="workspace_tree", mime_type="text/plain")
    async def tree(self) -> str:
        """A text tree of the workspace (first 1000 entries)."""
        entries = await self.list_dir(".", recursive=True)
        return (
            "\n".join(("📁 " if e["type"] == "dir" else "📄 ") + e["path"] for e in entries)
            or "(empty)"
        )
