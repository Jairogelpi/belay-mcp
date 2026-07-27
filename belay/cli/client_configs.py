"""Per-client MCP config rendering (adoption/DX, not spec-numbered).

Each client uses a genuinely different config format and file:

- Claude Desktop / Claude Code / Cursor: JSON, `{"mcpServers": {name:
  {"command", "args"}}}` (`belay/cli/main.py`'s existing `_register_client`).
- Codex CLI: TOML, `[mcp_servers.<name>]` tables with `command`/`args`
  keys, at either `.codex/config.toml` (project scope, default -- Codex's
  own project-config precedent) or `~/.codex/config.toml` (`--scope
  user`). Edited with `tomlkit`, a format-preserving TOML library --
  **not** a hand-rolled regex merge. An earlier version of this module did
  exactly that and was reproducibly broken by ordinary, valid TOML: a
  comment on the `[mcp_servers.<name>]` heading line, or an indented
  table immediately following it, could corrupt or silently delete
  unrelated content. `tomlkit` parses the real document structure (tables,
  comments, whitespace) and edits only the one table this module owns,
  so every other table survives byte-for-byte.
- OpenCode: JSON, `opencode.json`, `{"mcp": {name: {"type": "local",
  "command": [...]}}}` -- `command` is a single argv array here, not a
  split `command`/`args` pair like the others.

Each `render_*` function is pure (existing text in, new text out) so it's
testable without touching a real file; `belay/cli/main.py` does the actual
read/write (atomically, with a backup -- see `atomic_write_with_backup`).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import tomlkit


def render_codex_toml(existing: str, name: str, command: str, args: list[str]) -> str:
    """Merge a `[mcp_servers.<name>]` table into an existing (or empty) config.toml body,
    preserving every other table, comment, and formatting choice via `tomlkit`."""
    if existing.startswith("﻿"):  # tomlkit's parser rejects a leading BOM
        existing = existing[1:]
    doc = tomlkit.parse(existing) if existing.strip() else tomlkit.document()

    servers = doc.get("mcp_servers")
    if servers is None:
        servers = tomlkit.table()
        doc["mcp_servers"] = servers
    elif not isinstance(servers, (tomlkit.items.Table, dict)):
        raise ValueError("config.toml has a non-table 'mcp_servers' key -- fix by hand")

    entry = tomlkit.table()
    entry["command"] = command
    entry["args"] = args
    servers[name] = entry

    return tomlkit.dumps(doc)


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


def atomic_write_with_backup(target: Path, new_text: str) -> Path | None:
    """Write `new_text` to `target` atomically (temp file + `os.replace`), after
    backing up any existing content to `<target>.belay-backup` (private
    permissions, POSIX only -- Windows ACLs aren't touched, the file just isn't
    world-writable by default there either).

    Returns the backup path, or `None` if `target` didn't exist yet (nothing to
    back up). Never leaves `target` half-written: the temp file is renamed into
    place in one filesystem operation, so a crash mid-write leaves the original
    untouched, not corrupted.
    """
    backup_path: Path | None = None
    if target.is_file():
        backup_path = target.with_name(target.name + ".belay-backup")
        shutil.copy2(target, backup_path)
        if os.name == "posix":
            os.chmod(backup_path, 0o600)

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", text=True)
    import contextlib

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return backup_path
