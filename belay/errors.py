"""Belay error model (spec §11).

Defines the 17 error codes normative for 0.1, each with its `retryable`
flag. Full BelayError exception class and error-construction helpers
arrive with the components that raise them (E1+); this module fixes the
canonical registry early so every later entrega imports from one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

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


class BelayError(Exception):
    """Structured error per spec §11: `{"code", "detail", "retryable"}`.

    Raised at the boundaries components use to signal one of the 17
    normative error codes. `code` must be a key of `ERROR_CODES`; `retryable`
    defaults to the registry's value for that code but may be overridden.
    """

    def __init__(
        self,
        code: str,
        detail: Mapping[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown Belay error code: {code!r}")
        self.code = code
        self.detail: dict[str, Any] = dict(detail) if detail else {}
        self.retryable = ERROR_CODES[code] if retryable is None else retryable
        super().__init__(f"{code}: {self.detail}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "retryable": self.retryable}
