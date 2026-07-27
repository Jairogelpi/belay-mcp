"""`belay explore`: compare already-run session variants side by side, no verdict.

Belay does not generate variants -- it has no agent of its own to invoke;
it governs whatever agent calls it. "Explore" means: the human (or their
agent framework) already ran the same task N times, from the same
checkpoint, against sessions Belay governed -- `belay explore
<session_id>...` assembles one deterministic comparison table from data
these sessions already produced (the causal graph, plan effects, a real
rewind dry-run plan), same principle as `belay causal`/`export-pr`: real
data or an explicit "not measured", never a fabricated number.

No LLM crowns a winner (the pitch's own line: "Belay no dejaría que otro
LLM declarase mágicamente un ganador"). This prints a frontier; a human
picks.
"""

from __future__ import annotations

from dataclasses import dataclass

from belay.cli.causal import build_causal_graph
from belay.ledger.model import Event


@dataclass
class VariantMetrics:
    session_id: str
    steps_total: int
    files_touched: int
    tools_used: list[str]
    steps_proven: int
    steps_unproven: int
    unknown_effects: int
    irreversible_or_indeterminate: int | None  # None: not measured (no --config given)


def compute_metrics(
    session_id: str, events: list[Event], rewind_irreversible_count: int | None
) -> VariantMetrics:
    nodes = build_causal_graph(events)
    files = {n.args.get("path") for n in nodes if isinstance(n.args.get("path"), str)}
    tools = sorted({n.tool for n in nodes})
    proven = sum(1 for n in nodes if n.test_ref)
    unproven = len(nodes) - proven

    unknown_total = 0
    for event in events:
        if event.type == "plan_created":
            unknown_total += len(event.payload.get("unknown") or [])

    return VariantMetrics(
        session_id=session_id,
        steps_total=len(nodes),
        files_touched=len(files),
        tools_used=tools,
        steps_proven=proven,
        steps_unproven=unproven,
        unknown_effects=unknown_total,
        irreversible_or_indeterminate=rewind_irreversible_count,
    )


def render_table(metrics: list[VariantMetrics]) -> str:
    headers = [
        "session", "steps", "files", "proven", "unproven", "unknown_effects", "irreversible/indet.",
    ]
    rows = [
        [
            m.session_id,
            str(m.steps_total),
            str(m.files_touched),
            str(m.steps_proven),
            str(m.steps_unproven),
            str(m.unknown_effects),
            "not measured" if m.irreversible_or_indeterminate is None else str(m.irreversible_or_indeterminate),
        ]
        for m in metrics
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    lines = [
        "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)),
        "  ".join("-" * w for w in widths),
    ]
    for row in rows:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)))
    tools_lines = [f"{m.session_id}: tools used = {', '.join(m.tools_used) or '(none)'}" for m in metrics]
    return "\n".join(lines) + "\n\n" + "\n".join(tools_lines) + (
        "\n\nno verdict is computed -- pick from this evidence, or ask each session's "
        "`belay causal`/`belay export-pr` for detail before deciding."
    )
