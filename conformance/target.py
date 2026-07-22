"""`ConformanceTarget`: the thin adapter any Belay implementation implements
to be exercised by the public conformance suite (spec §13, plan.md E8).

Six methods, derived directly from what the L1/L2/L3 scenarios need to
drive and inspect:

- `new_session`   -- boot a governed session over a contract set with a
                      fake/real tool executor behind it (needed by every level).
- `call`          -- proxy one tool call through resolve->plan->policy->
                      (approval)->execute (§3-§7; L1/L2).
- `approve`       -- resolve a parked `pause` verdict as the *operator*,
                      never the agent (§7.2 no-self-approval; L2).
- `ledger`        -- read back the event stream for chain/coherence
                      verification (§9.1, §9.2; all levels).
- `run_saga`      -- run an ordered multi-step saga with optional
                      auto-compensation (§8; L3).
- `rewind`        -- rewind a session, forward or dry-run (§10; L3).

A target need not implement every method meaningfully for every level: an
L1-only implementation can raise `NotImplementedError` from `run_saga`/
`rewind`, and the suite simply won't request L3 markers against it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

Executor = Callable[[str, dict[str, Any]], Awaitable[Any]]


class ConformanceTarget(Protocol):
    """Adapter a conformance suite drives to exercise one Belay implementation."""

    def new_session(
        self,
        contract_paths: list[Path],
        executor: Executor,
        *,
        policy_path: Path | None = None,
    ) -> str:
        """Start a fresh governed session over `contract_paths`, return its session id."""
        ...

    async def call(
        self, session_id: str, tool: str, args: dict[str, Any], *, read_only_hint: bool = False
    ) -> Any:
        """Proxy one call through the full lifecycle (spec §3). Raises on error verdicts."""
        ...

    def approve(
        self, session_id: str, approval_id: str, *, approved_by: str = "conformance-suite"
    ) -> None:
        """Approve a parked item as the operator (spec §7.1). Never reachable from `call`."""
        ...

    def ledger(self, session_id: str) -> list[Any]:
        """The raw event stream for `session_id`, oldest first (spec §9.1)."""
        ...

    async def run_saga(
        self, session_id: str, steps: list[Any], *, auto_compensate: bool = True
    ) -> Any:
        """Run an ordered saga (spec §8.2); `steps` are implementation-native step specs."""
        ...

    async def rewind(
        self, session_id: str, *, dry_run: bool = False, by: str = "conformance-suite"
    ) -> Any:
        """Rewind `session_id` (spec §10)."""
        ...
