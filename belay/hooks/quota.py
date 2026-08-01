"""Per-OS-user rolling quota of approved hook-gated actions (R1 fourth
slice, ADR 0023).

Deliberately not a reuse of `belay/policy/quota.py::QuotaTracker` (E15):
that tracker is tightly coupled to the MCP proxy's ledger shapes
(`session_started.initiated_by`, `plan_created.reversibility`,
`policy_evaluated.verdict`, `step_committed`) -- none of which hook
events have. Native Agent Gate events have no `--initiated-by` identity
concept at all; the identity used here is `HookEvent.os_user` (obtained
from the OS itself, independent of any agent-supplied payload -- see
`belay/supervisor/protocol.py::local_os_user`), a deliberate, documented
design choice, not a silent assumption.

Semantics also differ on purpose from E15, not just in identity source:
E15 escalates an otherwise-`allow`ed irreversible action to `pause` once
volume crosses the threshold. In the hooks world, everything that would
need this tracker already pauses by default (an unrecognized Bash
command, a non-read-only native MCP call, an oversized file edit) -- there
is no "auto-allowed irreversible action" for quota to catch. So here,
quota escalates the *next* level instead: `pause` -> hard `deny`, once an
identity has accumulated too many *approved* actions in the window. The
one still open: stop asking, an operator must intervene directly instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from belay.clock import Clock, SystemClock
from belay.ledger.store import LedgerStore


@dataclass
class HookQuotaTracker:
    """Counts one OS user's approved hook-gated actions within a rolling window."""

    ledger: LedgerStore

    def count(self, os_user: str, *, now: datetime, window: timedelta) -> int:
        """Boundary rule matches `belay/policy/quota.py::QuotaTracker.count`:
        an event exactly `window` old still counts (`now - at <= window`);
        anything older does not."""
        cutoff = now - window
        events = self.ledger.read_all()

        approved_ids = {
            e.payload.get("approval_id")
            for e in events
            if e.type == "approval_resolved" and e.payload.get("state") == "approved"
        }

        count = 0
        for event in events:
            if event.type != "hook_pre_tool_use":
                continue
            if event.payload.get("os_user") != os_user:
                continue
            if event.payload.get("verdict") != "deny":
                continue
            approval_id = event.payload.get("approval_id")
            if approval_id is None or approval_id not in approved_ids:
                continue
            when = datetime.fromisoformat(event.at)
            if cutoff <= when <= now:
                count += 1
        return count


@dataclass(frozen=True)
class QuotaConfig:
    """Bundles what `belay/hooks/gate.py`'s pause paths need to enforce a
    quota, mirroring `belay hooks install --contracts`'s pointer-file
    pattern (ADR 0021): configured opt-in, off by default, loaded once by
    the supervisor at construction."""

    ledger: LedgerStore
    max_actions: int
    window: timedelta
    clock: Clock = field(default_factory=SystemClock)

    def exceeded_for(self, os_user: str) -> bool:
        count = HookQuotaTracker(self.ledger).count(
            os_user, now=self.clock.now(), window=self.window
        )
        return count >= self.max_actions
