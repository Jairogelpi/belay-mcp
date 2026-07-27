"""Heuristic contract drafting from a live upstream's MCP tool list.

Connects to a real upstream, reads each tool's name and MCP annotations
(`readOnlyHint`/`destructiveHint` -- no LLM, no inference from prose), and
proposes a contract per tool using the same read/write pairing pattern
already hand-written in `examples/contracts/fs.yaml`:

- read-only tools -> `reversible`, no-op undo (re-running the same read).
- write/delete tools with a same-resource read counterpart on the same
  server (`write_x` <-> `read_x`, `delete_x` <-> `read_x`) -> `reversible`/
  `conditional` via capture-before-write, undo = re-write the captured
  state, mirroring `fs.write_file`/`fs.delete_file`.
- everything else -> `irreversible` (the safe default when no undo path can
  be inferred).

Every draft carries `provenance.verified: false` and a `summary` flagging it
as heuristic -- this is a starting point for human review, never a contract
Belay will treat as reviewed. Guessing wrong here is not a safety issue
*because* nothing downstream trusts `verified: true` until a human sets it.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp.types import Tool

_READ_PREFIXES = ("read_", "get_", "list_", "fetch_")
_WRITE_PREFIXES = ("write_", "set_", "update_", "put_", "create_", "add_")
_DELETE_PREFIXES = ("delete_", "remove_", "unlink_")


def _strip_prefix(name: str, prefixes: tuple[str, ...]) -> str | None:
    for prefix in prefixes:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return None


def _find_read_counterpart(stem: str, all_names: set[str]) -> str | None:
    for prefix in _READ_PREFIXES:
        candidate = f"{prefix}{stem}"
        if candidate in all_names:
            return candidate
    return None


def _primary_arg(tool: Tool) -> str | None:
    """The first required argument name, used as the identifying key (e.g. `path`)."""
    schema = tool.inputSchema or {}
    required = schema.get("required") or []
    if required:
        return str(required[0])
    props = schema.get("properties") or {}
    return next(iter(props), None)


@dataclass
class DraftResult:
    tool_name: str
    document: dict[str, object]
    note: str


def draft_contract(tool: Tool, all_names: set[str]) -> DraftResult:
    """Propose one contract document for `tool` (spec §4 shape, `provenance.verified: false`)."""
    name = tool.name
    annotations = tool.annotations
    read_only = bool(annotations and annotations.readOnlyHint)
    key_arg = _primary_arg(tool)

    base: dict[str, object] = {
        "belay_contract": "0.1",
        "tool": name,
        "provenance": {"declared_by": "integrator", "verified": False},
    }

    if read_only:
        base["summary"] = f"Heuristic: read-only (MCP readOnlyHint) -- {tool.description or name}"
        base["reversibility"] = "reversible"
        base["undo"] = {
            "tool": name,
            "args": {key_arg: f"$args.{key_arg}"} if key_arg else {},
        }
        base["effects"] = [{"type": "read", "resource": name, "count": "1"}]
        return DraftResult(name, base, "read-only, no-op undo")

    write_stem = _strip_prefix(name, _WRITE_PREFIXES)
    delete_stem = _strip_prefix(name, _DELETE_PREFIXES)
    stem = write_stem if write_stem is not None else delete_stem

    if stem is not None and key_arg is not None:
        read_tool = _find_read_counterpart(stem, all_names)
        if read_tool is not None:
            base["summary"] = (
                f"Heuristic: paired with read tool '{read_tool}' on '{key_arg}' -- "
                f"{tool.description or name}"
            )
            base["capture"] = {
                "tool": read_tool,
                "args": {key_arg: f"$args.{key_arg}"},
                "as": "before",
            }
            if delete_stem is not None:
                base["reversibility"] = "conditional"
                base["conditions"] = ["$state.before != null"]
            else:
                base["reversibility"] = "reversible"
            base["undo"] = {
                "tool": name if write_stem is not None else read_tool,
                "args": {key_arg: f"$args.{key_arg}", "content": "$state.before.content"},
            }
            base["effects"] = [
                {
                    "type": "delete" if delete_stem is not None else "update",
                    "resource": name,
                    "count": "1",
                }
            ]
            return DraftResult(
                name, base, f"paired with read tool '{read_tool}' -- verify undo.args mapping"
            )

    destructive = bool(annotations and annotations.destructiveHint)
    base["summary"] = (
        f"Heuristic: no undo path found ({'destructive' if destructive else 'unclassified'} "
        f"MCP hints) -- {tool.description or name}"
    )
    base["reversibility"] = "irreversible"
    base["effects"] = [{"type": "execute", "resource": name, "count": "1"}]
    return DraftResult(name, base, "no read counterpart found -- defaulted to irreversible")


def draft_contracts(tools: list[Tool]) -> list[DraftResult]:
    all_names = {t.name for t in tools}
    return [draft_contract(tool, all_names) for tool in tools]
