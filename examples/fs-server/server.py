"""Toy MCP filesystem server (examples/fs-server, plan.md §2).

A minimal, real MCP server exposing list/read/write/delete over a sandboxed
directory. Used to exercise the Belay L1 proxy end-to-end (E3): this is the
upstream tool server that `belay wrap`/`belay run` sit in front of.

The sandbox root is `BELAY_FS_ROOT` if set, else a temp directory created on
startup. All paths are resolved relative to the root and a call outside the
root is rejected — this is a toy server, not a security boundary for
untrusted input, but it keeps the demo/tests from touching the real
filesystem.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

_ROOT = Path(os.environ.get("BELAY_FS_ROOT") or tempfile.mkdtemp(prefix="belay-fs-")).resolve()
_ROOT.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("belay-fs-server")


def _resolve(path: str) -> Path:
    candidate = (_ROOT / path).resolve()
    if _ROOT not in candidate.parents and candidate != _ROOT:
        raise ValueError(f"path escapes sandbox root: {path!r}")
    return candidate


@mcp.tool(name="fs.list_files", annotations=ToolAnnotations(readOnlyHint=True))
def list_files() -> list[str]:
    """List every file in the sandbox, relative to its root."""
    return sorted(
        str(p.relative_to(_ROOT)) for p in _ROOT.rglob("*") if p.is_file()
    )


@mcp.tool(name="fs.read_file", annotations=ToolAnnotations(readOnlyHint=True))
def read_file(path: str) -> str:
    """Read a file's contents as text."""
    target = _resolve(path)
    if not target.exists():
        raise FileNotFoundError(path)
    return target.read_text(encoding="utf-8")


@mcp.tool(name="fs.write_file")
def write_file(path: str, content: str) -> dict[str, object]:
    """Overwrite (or create) a file with `content`. Not read-only, not marked destructive."""
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": path, "bytes": len(content.encode("utf-8"))}


@mcp.tool(name="fs.delete_file", annotations=ToolAnnotations(destructiveHint=True))
def delete_file(path: str) -> dict[str, object]:
    """Delete a file. Marked destructive but a hint never authorizes on its own (spec §4.6)."""
    target = _resolve(path)
    existed = target.exists()
    if existed:
        target.unlink()
    return {"path": path, "existed": existed}


if __name__ == "__main__":
    mcp.run()
