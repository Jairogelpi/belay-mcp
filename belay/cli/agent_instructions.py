"""Standing-instruction snippet for AGENTS.md / CLAUDE.md (adoption/DX, not spec-numbered).

Registering Belay in an MCP client's config (`belay init`) makes the tool
*available*; it doesn't make an agent reach for it by default instead of
whatever direct tool it already knows. `AGENTS.md` (Codex, opencode, and
increasingly a cross-tool convention) and `CLAUDE.md` (Claude Code) are the
one mechanism that's actually read automatically, every session, without
the user repeating themselves -- so that's where a durable "use Belay,
don't ask" instruction has to live, not in a one-off chat message.
"""

from __future__ import annotations

_MARKER_START = "<!-- belay:standing-instruction:start -->"
_MARKER_END = "<!-- belay:standing-instruction:end -->"


def render_snippet(server_name: str = "belay") -> str:
    return f"""{_MARKER_START}
## Tool execution goes through Belay

This project has the `{server_name}` MCP server registered
(`belay init`). For every tool call that Belay wraps (see its
`mcpServers` entry / `belay.wrap.json` for the exact set), **use the
`{server_name}` MCP tools, not a direct/unwrapped equivalent** -- do this
by default, without being asked each time. Belay gives each call a
declared reversibility, a policy check, and an append-only ledger entry
before it runs; skipping it for a wrapped tool skips that governance.

If a call Belay governs comes back `pending_approval`, stop and tell the
user rather than retrying, escalating privileges, or working around it --
approval is intentionally a human, CLI-only action (`belay approvals`),
never something the agent can grant itself.
{_MARKER_END}"""


def upsert(existing: str, server_name: str = "belay") -> str:
    """Insert or replace the Belay block in an existing AGENTS.md/CLAUDE.md body."""
    snippet = render_snippet(server_name)
    if _MARKER_START in existing and _MARKER_END in existing:
        start = existing.index(_MARKER_START)
        end = existing.index(_MARKER_END) + len(_MARKER_END)
        return existing[:start] + snippet + existing[end:]
    sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
    return f"{existing}{sep}{snippet}\n"
