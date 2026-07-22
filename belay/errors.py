"""Belay error model (spec §11).

Defines the 17 error codes normative for 0.1, each with its `retryable`
flag. Full BelayError exception class and error-construction helpers
arrive with the components that raise them (E1+); this module fixes the
canonical registry early so every later entrega imports from one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

# code -> retryable (spec §11)
ERROR_CODES: Final[Mapping[str, bool]] = {
    "contract_missing": False,
    "contract_invalid": False,
    "expression_invalid": False,
    "capture_failed": False,
    "plan_expired": True,
    "plan_mismatch": False,
    "policy_denied": False,
    "approval_required": True,
    "approval_rejected": False,
    "approval_expired": False,
    "idempotency_conflict": False,
    "step_indeterminate": False,
    "compensation_failed": False,
    "verification_failed": False,
    "session_fenced": False,
    "ledger_integrity_error": False,
    "unsafe_passthrough_disabled": False,
}
