"""belay/ledger/model.py: the step-outcome event-type constants (R1.7.2,
ADR 0025) -- one canonical source shared by the writers
(belay/executor/saga.py, belay/executor/recovery.py) and the readers that
classify by them (belay/rewind/service.py, belay/cli/causal.py), instead
of three independently-typed string literals with no compiler-enforced
link between producer and consumer.
"""

from __future__ import annotations

from belay.ledger.model import (
    EVENT_TYPES,
    STEP_COMMITTED,
    STEP_FAILED,
    STEP_INDETERMINATE,
)


def test_step_outcome_constants_match_their_string_values() -> None:
    assert STEP_COMMITTED == "step_committed"
    assert STEP_FAILED == "step_failed"
    assert STEP_INDETERMINATE == "step_indeterminate"


def test_step_outcome_constants_are_all_present_in_event_types() -> None:
    """These are additive aliases into `EVENT_TYPES` (spec §9.1's
    normative list), not a parallel/competing definition -- every
    constant must resolve to a real, normative event type."""
    for constant in (STEP_COMMITTED, STEP_FAILED, STEP_INDETERMINATE):
        assert constant in EVENT_TYPES
