"""`belay learn <approval_id>`: compile a rejection into a durable rule (adoption/DX).

"Not agent memory. Learning compiled into enforceable controls" -- but
compiling *what*, mechanically, from a free-text rejection reason, without
an LLM interpreting it? Only two proposals are safe to generate
deterministically from an `ApprovalItem` alone:

- forbid the rejected tool outright (`forbidden_tools`).
- forbid the rejected call's file scope (`forbidden_scope`, if its plan
  touched a `path`).

Either is a real, mechanical addition to an `IntentContract`
(`belay/intent/model.py`) -- already enforced by every future session
that loads it (`belay/intent/enforce.py`), so this isn't a suggestion
Belay merely remembers, it's a rule the next session actually can't
violate. Never applied silently: `belay learn` only *proposes*; a human
must rerun with `--apply` naming the file to change. Nothing narrower or
smarter (e.g. "forbid this only when combined with X") is attempted --
that would require interpreting the free-text rejection reason, which is
exactly the kind of judgment Belay's discipline keeps off the safety path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from belay.approvals.queue import ApprovalItem
from belay.intent.loader import load_intent_contract
from belay.intent.model import IntentContract


@dataclass(frozen=True)
class LearnedRule:
    kind: str  # "forbidden_tools" | "forbidden_scope"
    value: str
    reason: str


def propose_rule(item: ApprovalItem) -> list[LearnedRule]:
    """One or two candidate rules a human can choose to make durable. Never auto-picks one."""
    if item.state != "rejected":
        raise ValueError(f"approval {item.approval_id!r} is {item.state!r}, not 'rejected'")

    tool = item.plan.get("tool", "")
    reason = item.reason or "(no reason recorded)"
    rules = [LearnedRule("forbidden_tools", tool, reason)]

    path = (item.plan.get("args") or {}).get("path")
    if isinstance(path, str):
        parent = "/".join(path.split("/")[:-1])
        pattern = f"{parent}/**" if parent else path
        rules.append(LearnedRule("forbidden_scope", pattern, reason))

    return rules


def apply_rule(contract_path: str, rule: LearnedRule) -> IntentContract:
    """Add `rule` to the intent contract at `contract_path` (created fresh if missing)."""
    path = Path(contract_path)
    if path.is_file():
        contract = load_intent_contract(path)
    else:
        contract = IntentContract(intent="(no intent declared -- edit this by hand)")

    if rule.kind == "forbidden_tools" and rule.value not in contract.forbidden_tools:
        contract.forbidden_tools = [*contract.forbidden_tools, rule.value]
    elif rule.kind == "forbidden_scope" and rule.value not in contract.forbidden_scope:
        contract.forbidden_scope = [*contract.forbidden_scope, rule.value]

    import yaml

    path.write_text(
        yaml.safe_dump(contract.model_dump(), sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return contract
