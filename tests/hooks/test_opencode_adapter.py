"""belay/hooks/opencode_adapter.py: normalize/render only (E18.6) -- see
the module's own docstring for why there's no live plugin wiring yet.
Fixture (input, output) pairs below match the real trigger call shape
confirmed both from an actually-installed OpenCode plugin
(~/.config/opencode/plugins/engram.ts) and by locating the literal
`W.trigger("tool.execute.before"/"after", ...)` call sites inside the real
installed opencode-ai 1.14.33 Windows binary.
"""

from __future__ import annotations

import pytest
from belay.hooks.gate import GateDecision, evaluate
from belay.hooks.opencode_adapter import (
    normalize_tool_execute_after,
    normalize_tool_execute_before,
    render_tool_execute_before_decision,
)


class TestNormalizeToolExecuteBefore:
    def test_bash_tool_maps_to_shell_surface(self) -> None:
        event = normalize_tool_execute_before(
            {"tool": "bash", "sessionID": "ses_1", "callID": "call_1"},
            {"args": {"command": "git status"}},
            installation_id="i",
        )
        assert event.surface == "shell"
        assert event.tool_name == "Bash"
        assert event.args["command"] == "git status"
        assert event.host == "opencode"
        assert event.host_session_id == "ses_1"
        assert event.event_id == "call_1"

    def test_edit_tool_maps_to_file_surface(self) -> None:
        event = normalize_tool_execute_before(
            {"tool": "edit", "sessionID": "ses_1", "callID": "call_2"},
            {"args": {"filePath": "/repo/f.txt", "newString": "x"}},
            installation_id="i",
        )
        assert event.surface == "file"
        assert event.tool_name == "edit"
        assert event.args == {"filePath": "/repo/f.txt", "newString": "x"}

    def test_unrecognized_tool_maps_to_other(self) -> None:
        event = normalize_tool_execute_before(
            {"tool": "webfetch", "sessionID": "ses_1", "callID": "call_3"},
            {"args": {"url": "https://example.com"}},
            installation_id="i",
        )
        assert event.surface == "other"

    def test_trust_tier_is_unknown(self) -> None:
        event = normalize_tool_execute_before(
            {"tool": "bash", "sessionID": "s", "callID": "c"},
            {"args": {"command": "ls"}},
            installation_id="i",
        )
        assert event.trust_tier == "UNKNOWN"

    def test_missing_tool_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_tool_execute_before(
                {"sessionID": "s", "callID": "c"}, {"args": {}}, installation_id="i"
            )

    def test_missing_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_tool_execute_before(
                {"tool": "bash", "callID": "c"}, {"args": {}}, installation_id="i"
            )

    def test_missing_args_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_tool_execute_before(
                {"tool": "bash", "sessionID": "s", "callID": "c"}, {}, installation_id="i"
            )

    def test_the_normalized_event_runs_through_the_real_bash_gate(self) -> None:
        """End-to-end within this test process: an OpenCode-shaped
        tool.execute.before call, once normalized, is indistinguishable to
        gate.evaluate() from a Claude Code one -- the host-agnostic design
        pays off for a third host too."""
        from belay.approvals.queue import ApprovalQueue

        queue = ApprovalQueue()
        safe = normalize_tool_execute_before(
            {"tool": "bash", "sessionID": "s", "callID": "c1"},
            {"args": {"command": "git status"}},
            installation_id="i",
        )
        assert evaluate(safe, queue).verdict == "allow"

        risky = normalize_tool_execute_before(
            {"tool": "bash", "sessionID": "s", "callID": "c2"},
            {"args": {"command": "rm -rf /tmp/x"}},
            installation_id="i",
        )
        result = evaluate(risky, queue)
        assert result.verdict == "deny"
        assert len(queue.list()) == 1


class TestRenderToolExecuteBeforeDecision:
    def test_allow(self) -> None:
        assert render_tool_execute_before_decision(GateDecision("allow", "belay: safe")) == {
            "action": "allow"
        }

    def test_deny_carries_the_reason(self) -> None:
        out = render_tool_execute_before_decision(GateDecision("deny", "belay: paused"))
        assert out == {"action": "deny", "reason": "belay: paused"}


class TestNormalizeToolExecuteAfter:
    def test_captures_exit_code_and_result_status(self) -> None:
        event = normalize_tool_execute_after(
            {"tool": "bash", "sessionID": "s", "callID": "c", "args": {"command": "git status"}},
            {"stdout": "clean", "exitCode": 0},
            installation_id="i",
        )
        assert event.phase == "post"
        assert event.exit_code == 0
        assert event.result_status == "success"
        assert event.output_digest is not None

    def test_nonzero_exit_code_is_failure(self) -> None:
        event = normalize_tool_execute_after(
            {"tool": "bash", "sessionID": "s", "callID": "c", "args": {"command": "false"}},
            {"stdout": "", "exitCode": 1},
            installation_id="i",
        )
        assert event.result_status == "failure"

    def test_string_output_still_produces_a_digest(self) -> None:
        event = normalize_tool_execute_after(
            {"tool": "edit", "sessionID": "s", "callID": "c", "args": {"filePath": "f"}},
            "ok",
            installation_id="i",
        )
        assert event.output_digest is not None
        assert event.exit_code is None  # never fabricated from a bare string

    def test_missing_tool_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_tool_execute_after(
                {"sessionID": "s", "callID": "c"}, {}, installation_id="i"
            )
