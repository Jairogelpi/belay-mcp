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

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
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


def sha256_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest_path(target: Path) -> Path:
    return target.with_name(target.name + ".belay-manifest.json")


@dataclass(frozen=True)
class Manifest:
    client: str
    target: str
    name: str
    before_hash: str | None  # None: target didn't exist before this install
    after_hash: str
    backup_path: str | None
    installed_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "client": self.client,
            "target": self.target,
            "name": self.name,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "backup_path": self.backup_path,
            "installed_at": self.installed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Manifest:
        return cls(
            client=str(data["client"]),
            target=str(data["target"]),
            name=str(data["name"]),
            before_hash=data.get("before_hash"),  # type: ignore[arg-type]
            after_hash=str(data["after_hash"]),
            backup_path=data.get("backup_path"),  # type: ignore[arg-type]
            installed_at=str(data["installed_at"]),
        )


def write_manifest(client: str, target: Path, name: str, before_hash: str | None, after_text: str,
                    backup_path: Path | None) -> Path:
    """Record what this install just did -- the one source `belay
    uninstall`/`belay doctor` trust to know whether the file has changed since,
    without guessing from content alone.

    `before_hash` (not raw text) so a reinstall over an unmodified belay-managed
    config can pass through the *original* pre-install hash from the prior
    manifest, rather than hashing the already-belay-containing content that was
    on disk immediately before this write -- see `_register_client`."""
    manifest = Manifest(
        client=client,
        target=str(target),
        name=name,
        before_hash=before_hash,
        after_hash=sha256_of(after_text),
        backup_path=str(backup_path) if backup_path else None,
        installed_at=datetime.now(UTC).isoformat(),
    )
    path = manifest_path(target)
    path.write_text(json.dumps(manifest.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def load_manifest(target: Path) -> Manifest | None:
    path = manifest_path(target)
    if not path.is_file():
        return None
    return Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))


#: Marker substring used to recognize a belay-managed hook entry inside
#: settings.json's `hooks.<event>` array, so a reinstall replaces it in place
#: (idempotent, no duplicate entries) and an uninstall removes exactly the
#: entries belay added -- without needing a table/key to scope the edit to,
#: the way the MCP-registration renderers above do (`hooks.<event>` is an
#: array of arbitrary shape, not a name-keyed object).
#:
#: NOT "belay hooks run" -- the actual command belay installs is
#: `"<python>" -m belay.cli.main hooks run <event> --db ...` (see
#: `_hooks_command_for` in belay/cli/main.py), which contains "hooks run"
#: but never the contiguous substring "belay hooks run" ("belay.cli.main
#: hooks run", not "belay hooks run"). That mismatch was caught by
#: `tests/cli/test_hooks_lifecycle.py::test_doctor_reports_unchanged_since_install`
#: failing immediately after a fresh install -- belay couldn't recognize its
#: own just-installed entry as its own.
_BELAY_HOOK_MARKER = "hooks run"


def _is_belay_hook_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    for h in entry.get("hooks", []):
        if isinstance(h, dict) and _BELAY_HOOK_MARKER in str(h.get("command", "")):
            return True
    return False


def render_claude_hooks_settings(
    existing: str, command: str, event: str = "PreToolUse", matcher: str = "Bash"
) -> str:
    """Merge a belay-managed hook entry into settings.json's `hooks.<event>`
    array. Idempotent: an existing belay entry (recognized by
    `_BELAY_HOOK_MARKER` in its command) is replaced in place, never
    duplicated on reinstall; any other entries already there (the user's own
    hooks, or belay entries for a different event) are left untouched."""
    doc: dict[str, object] = json.loads(existing) if existing.strip() else {}
    hooks = doc.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.json has a non-object 'hooks' key -- fix by hand")
    event_list = hooks.get(event, [])
    if not isinstance(event_list, list):
        raise ValueError(f"settings.json's hooks.{event} is not an array -- fix by hand")

    new_list = [entry for entry in event_list if not _is_belay_hook_entry(entry)]
    new_list.append({"matcher": matcher, "hooks": [{"type": "command", "command": command}]})
    hooks[event] = new_list
    return json.dumps(doc, indent=2) + "\n"


def remove_claude_hooks_entry(existing: str, event: str = "PreToolUse") -> str:
    """Surgically remove belay's entry (only) from settings.json's
    `hooks.<event>` array, leaving any other hooks in that array -- and every
    other event/key in the file -- untouched."""
    doc: dict[str, object] = json.loads(existing) if existing.strip() else {}
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return json.dumps(doc, indent=2) + "\n"
    event_list = hooks.get(event)
    if isinstance(event_list, list):
        hooks[event] = [entry for entry in event_list if not _is_belay_hook_entry(entry)]
    return json.dumps(doc, indent=2) + "\n"


def claude_hooks_entry_present(existing: str, event: str = "PreToolUse") -> bool:
    if not existing.strip():
        return False
    doc = json.loads(existing)
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return False
    event_list = hooks.get(event)
    if not isinstance(event_list, list):
        return False
    return any(_is_belay_hook_entry(entry) for entry in event_list)


def entry_present(client: str, existing: str, name: str) -> bool:
    """Whether `name`'s entry is actually present in `existing`, parsed per
    `client`'s config format. `belay doctor` uses this to catch a manifest
    that no longer reflects reality -- e.g. the belay entry (or the whole
    relevant table/key) was hand-edited out without going through `belay
    uninstall` -- and report BROKEN instead of claiming it's still registered."""
    if not existing.strip():
        return False
    if client == "codex":
        doc = tomlkit.parse(existing)
        servers = doc.get("mcp_servers")
        return isinstance(servers, (tomlkit.items.Table, dict)) and name in servers
    if client == "opencode":
        doc = json.loads(existing)
        mcp = doc.get("mcp")
        return isinstance(mcp, dict) and name in mcp
    doc = json.loads(existing)
    servers = doc.get("mcpServers")
    return isinstance(servers, dict) and name in servers


def remove_codex_entry(existing: str, name: str) -> str:
    """Surgically remove `[mcp_servers.<name>]` from a config.toml body, leaving
    everything else untouched -- used when the file changed after install (so
    restoring the pre-install backup would discard the user's other edits)."""
    doc = tomlkit.parse(existing)
    servers = doc.get("mcp_servers")
    if isinstance(servers, (tomlkit.items.Table, dict)) and name in servers:
        del servers[name]
    return tomlkit.dumps(doc)


def remove_json_mcp_entry(existing: str, name: str, key: str = "mcpServers") -> str:
    """Surgically remove one entry from a JSON `mcpServers`/`mcp` object, leaving
    everything else (other servers, unrelated top-level keys) untouched."""
    doc: dict[str, object] = json.loads(existing) if existing.strip() else {}
    servers = doc.get(key)
    if isinstance(servers, dict) and name in servers:
        del servers[name]
    return json.dumps(doc, indent=2) + "\n"


def atomic_write(target: Path, new_text: str) -> None:
    """Write `new_text` to `target` atomically (temp file + `os.replace`), no backup.
    Never leaves `target` half-written: the temp file is renamed into place in one
    filesystem operation, so a crash mid-write leaves the original untouched, not
    corrupted."""
    import contextlib

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_with_backup(target: Path, new_text: str) -> Path | None:
    """Write `new_text` to `target` atomically (temp file + `os.replace`), after
    backing up any existing content to `<target>.belay-backup` (private
    permissions, POSIX only -- Windows ACLs aren't touched, the file just isn't
    world-writable by default there either).

    Returns the backup path, or `None` if `target` didn't exist yet (nothing to
    back up).
    """
    backup_path: Path | None = None
    if target.is_file():
        backup_path = target.with_name(target.name + ".belay-backup")
        shutil.copy2(target, backup_path)
        if os.name == "posix":
            os.chmod(backup_path, 0o600)

    atomic_write(target, new_text)
    return backup_path


def atomic_restore(target: Path, backup: Path) -> None:
    """Restore `backup` onto `target`, byte-for-byte (binary copy, not text
    decode/encode -- avoids newline translation or encoding-error surprises) and
    atomically (temp file + `os.replace`, matching `atomic_write_with_backup`'s
    crash-safety: a failure mid-restore leaves `target` untouched)."""
    import contextlib

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(backup.read_bytes())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
