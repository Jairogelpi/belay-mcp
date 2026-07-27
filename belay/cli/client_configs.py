"""Per-client MCP config rendering (adoption/DX, not spec-numbered).

Each client uses a genuinely different config format and file -- this
isn't cosmetic, so there's no single "mcpServers" writer that covers all
of them:

- Claude Desktop / Claude Code / Cursor: JSON, `{"mcpServers": {name:
  {"command", "args"}}}` (`belay/cli/main.py`'s existing `_register_client`).
- Codex CLI: TOML, `~/.codex/config.toml`, `[mcp_servers.<name>]` tables
  with `command`/`args` keys. Python's stdlib `tomllib` is read-only (no
  writer) -- rather than add a TOML-writing dependency for one table shape,
  this does a targeted text merge: replace the `[mcp_servers.<name>]`
  section if a heading with that exact name exists, otherwise append one.
  Every other section/table in the file is left untouched by construction
  (only text between this table's heading and the next `[` heading, or
  EOF, is ever touched).
- OpenCode: JSON, `opencode.json`, `{"mcp": {name: {"type": "local",
  "command": [...]}}}` -- `command` is a single argv array here, not a
  split `command`/`args` pair like the others.

Each `render_*` function is pure (existing text in, new text out) so it's
testable without touching a real file; `belay/cli/main.py` does the actual
read/write.
"""

from __future__ import annotations

import json
import re


def render_codex_toml(existing: str, name: str, command: str, args: list[str]) -> str:
    """Merge a `[mcp_servers.<name>]` table into an existing (or empty) config.toml body."""
    args_toml = "[" + ", ".join(json.dumps(a) for a in args) + "]"
    block = f'[mcp_servers.{name}]\ncommand = {json.dumps(command)}\nargs = {args_toml}\n'

    heading = re.escape(f"[mcp_servers.{name}]")
    # Match from this table's heading up to (not including) the next `[section]`
    # heading or end of file -- never touches any other table.
    pattern = re.compile(rf"{heading}\n(?:(?!\n\[).*\n?)*", re.MULTILINE)
    if pattern.search(existing):
        return pattern.sub(block, existing, count=1)
    if not existing.strip():
        return block
    trimmed = existing.rstrip("\n")
    return f"{trimmed}\n\n{block}"


def render_opencode_json(existing: str, name: str, command: list[str]) -> str:
    """Merge an `mcp.<name>` entry into an existing (or empty) opencode.json body."""
    doc: dict[str, object] = {}
    if existing.strip():
        doc = json.loads(existing)
    mcp = doc.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise ValueError("opencode.json has a non-object 'mcp' key -- fix by hand")
    mcp[name] = {"type": "local", "command": command}
    return json.dumps(doc, indent=2) + "\n"
