"""belay/action_envelope.py: ActionEnvelope (R1.7.3, ADR 0025) -- the
shared shape both decision engines' per-call inputs already normalize
into. Purely additive: these tests only confirm the conversion functions
are accurate, not that anything production calls them (nothing does,
by design -- see the module docstring).
"""

from __future__ import annotations

from belay.action_envelope import ActionEnvelope, from_hook_event, from_mcp_call
from belay.hooks.claude_code_adapter import normalize


def _real_hook_event():
    raw = {
        "session_id": "sess-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "toolu_abc",
        "cwd": "/repo-a",
    }
    return normalize(raw, installation_id="test-install")


def test_from_hook_event_maps_every_field() -> None:
    event = _real_hook_event()
    envelope = from_hook_event(event)

    assert envelope.surface == event.surface
    assert envelope.host == event.host
    assert envelope.tool == event.tool_name
    assert envelope.args == event.args
    assert envelope.session_id == event.host_session_id
    assert envelope.cwd == event.cwd
    assert envelope.repo_prestate_digest == event.repo_identity
    assert envelope.os_identity == event.os_user
    assert envelope.event_id == event.event_id
    assert envelope.monotonic_ns == event.monotonic_ns
    assert envelope.wall_clock == event.wall_clock


def test_from_mcp_call_event_id_matches_the_lease_key_ApprovalStage_builds() -> None:
    """R1.7.1's `ApprovalStage.check()` keys `ApprovalQueue.consume()` by
    `f"{session_id}:{step_seq}"` -- `from_mcp_call` must produce the
    identical string, not a similar-looking one, proving both engines'
    "what identifies this call" concept is already the same shape."""
    envelope = from_mcp_call(
        session_id="s1", step_seq=3, tool="mail.send", args={"to": "a@example.com"}
    )
    assert envelope.event_id == "s1:3"


def test_from_mcp_call_is_honest_about_missing_concepts() -> None:
    """The MCP proxy has no repo-prestate or OS-identity concept today --
    both must stay `None`, never papered over with a value that doesn't
    mean the same thing as the hooks side's."""
    envelope = from_mcp_call(session_id="s1", step_seq=1, tool="mail.send", args={})
    assert envelope.repo_prestate_digest is None
    assert envelope.os_identity is None
    assert envelope.surface == "mcp"
    assert envelope.host == "mcp"


def test_action_envelope_is_frozen() -> None:
    envelope = from_mcp_call(session_id="s1", step_seq=1, tool="mail.send", args={})
    assert isinstance(envelope, ActionEnvelope)
    try:
        envelope.tool = "other"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("ActionEnvelope must be frozen")
