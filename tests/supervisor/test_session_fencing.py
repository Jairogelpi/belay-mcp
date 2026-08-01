"""belay/supervisor/server.py::Supervisor._decide_pre -- R1 third slice
(ADR 0021): a session fenced via `belay hooks fence` must deny every
surface (Bash, file edit, native MCP), checked once before any
surface-specific dispatch -- fencing is a ledger fact, exactly like
`belay/rewind/service.py::is_fenced()` already is for the MCP proxy path.
"""

from __future__ import annotations

from pathlib import Path

from belay.hooks.claude_code_adapter import normalize
from belay.hooks.gate import session_key
from belay.supervisor.addressing import SupervisorIdentity, supervisor_identity
from belay.supervisor.protocol import HookEvent
from belay.supervisor.server import Supervisor


def _identity(tmp_path: Path) -> SupervisorIdentity:
    project_anchor = (tmp_path / "belay-hooks.db").resolve()
    return supervisor_identity(project_anchor, belay_home=tmp_path / "home")


def _event(tool_name: str, tool_input: dict, session_id: str = "sess-fence") -> HookEvent:
    raw = {
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": f"toolu_{tool_name}",
    }
    return normalize(raw, installation_id="test-install")


def test_unfenced_session_is_decided_normally(tmp_path: Path) -> None:
    supervisor = Supervisor(_identity(tmp_path))
    result = supervisor._decide_pre(_event("Bash", {"command": "git status"}))
    assert result.verdict == "allow"


def test_fenced_session_denies_bash(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    supervisor = Supervisor(identity)
    supervisor._ledger.append(session_key("claude-code", "sess-fence"), "session_fenced", {})

    result = supervisor._decide_pre(_event("Bash", {"command": "git status"}))
    assert result.verdict == "deny"
    assert "fenced" in result.reason


def test_fenced_session_denies_file_edits_too(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    supervisor = Supervisor(identity)
    supervisor._ledger.append(session_key("claude-code", "sess-fence"), "session_fenced", {})

    result = supervisor._decide_pre(_event("Write", {"file_path": str(tmp_path / "f.txt")}))
    assert result.verdict == "deny"
    assert "fenced" in result.reason


def test_fenced_session_denies_native_mcp_calls_too(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    supervisor = Supervisor(identity)
    supervisor._ledger.append(session_key("claude-code", "sess-fence"), "session_fenced", {})

    result = supervisor._decide_pre(_event("mcp__github__list_issues", {}))
    assert result.verdict == "deny"
    assert "fenced" in result.reason


def test_fencing_one_session_does_not_affect_a_different_session(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    supervisor = Supervisor(identity)
    supervisor._ledger.append(session_key("claude-code", "sess-fence"), "session_fenced", {})

    other = _event("Bash", {"command": "git status"}, session_id="a-different-session")
    result = supervisor._decide_pre(other)
    assert result.verdict == "allow"
