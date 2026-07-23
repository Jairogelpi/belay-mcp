"""Spec MUST -> test traceability matrix generator (docs/plan.md §8).

Scans `tests/` and `conformance/tests/` for `@spec("X.Y")` markers in test
docstrings, cross-references them against a hand-curated list of every
normative MUST in `docs/spec.md`, and fails loudly if any MUST has zero
covering tests.

Usage:
    python scripts/traceability.py            # verify + write docs/traceability.md
    python scripts/traceability.py --check     # verify only, do not write the file

Exit code is non-zero (and a message names the uncovered MUST) if coverage
is incomplete.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_ROOTS = ["tests", "conformance/tests"]

_SPEC_MARKER_RE = re.compile(r'@spec\("([0-9]+(?:\.[0-9]+){0,2})"\)')


@dataclass(frozen=True)
class Must:
    id: str
    section: str
    text: str


# Hand-curated, reviewed list of distinct traceable requirements extracted
# from every normative "MUST" in docs/spec.md (E0-style extraction, updated
# here in the traceability entrega -- see docs/adr/0018-traceability-matrix.md
# for methodology). Updating this list is part of changing the spec.
MUSTS: list[Must] = [
    Must(
        "3.1",
        "3",
        "Every numbered lifecycle stage MUST emit its ledger event, even on denial/error.",
    ),
    Must(
        "4.2.1",
        "4.2",
        "`reversible` contracts MUST declare an `undo` block that fully negates the effects.",
    ),
    Must("4.2.2", "4.2", "`irreversible` contracts MUST NOT declare an `undo` block."),
    Must(
        "4.2.3",
        "4.2",
        "`conditional` contracts MUST include `undo` and `conditions`; unmet "
        "conditions at execution MUST be recorded as irreversible.",
    ),
    Must(
        "4.3",
        "4.3",
        "Implementations MUST reject any construct outside the expression "
        "grammar (no calls, no dunder, no eval/exec).",
    ),
    Must("4.4.1", "4.4", "The `capture` call MUST be read-only."),
    Must(
        "4.4.2",
        "4.4",
        "The `capture` call MUST execute before the main call, within the same step.",
    ),
    Must(
        "4.5",
        "4.5",
        "With `idempotency_key` declared, a repeat call with the same key MUST "
        "return the recorded result without re-calling the tool.",
    ),
    Must(
        "4.6.1",
        "4.6",
        "A tool with no contract and no `readOnlyHint` MUST be refused with `contract_missing`.",
    ),
    Must("4.6.2", "4.6", "`unsafe_passthrough` MUST be recorded in every affected ledger event."),
    Must(
        "4.7",
        "4.7",
        "A session MUST pin the `set_hash` present at `session_started`; "
        "later contract changes do not apply mid-session.",
    ),
    Must("5.3", "5.3", "Implementations MUST NOT present `contract`-basis counts as exact."),
    Must(
        "5.4.1",
        "5.4",
        "Executing against a prior `plan_id` MUST re-validate that args are "
        "byte-identical (else `plan_mismatch`).",
    ),
    Must("5.4.2", "5.4", "Expired plans MUST be re-planned (`plan_expired` on stale execution)."),
    Must("6.2", "6.2", "Every policy verdict MUST be recorded with the rule ids that fired."),
    Must(
        "6.3",
        "6.3",
        "Caps MUST be evaluated against the upper bound for `unknown`/`estimate` "
        "effects (worst-case).",
    ),
    Must("7.1", "7.1", "An expired approval item MUST NOT be executable."),
    Must(
        "7.2.1",
        "7.2",
        "The approving principal MUST be authenticated by the embedding system "
        "and MUST be recorded (`approved_by`).",
    ),
    Must(
        "7.2.2",
        "7.2",
        "An agent MUST NOT be able to approve its own actions through any tool Belay exposes.",
    ),
    Must(
        "8.1",
        "8.1",
        "On recovery, Belay MUST reconcile a journaled-but-unresolved step via "
        "the idempotency key, or mark it `indeterminate`.",
    ),
    Must(
        "9.2",
        "9.2",
        "A `verify` operation MUST recompute the hash chain and cross-check "
        "step coherence (journal/capture/result/compensation).",
    ),
    Must(
        "9.3",
        "9.3",
        "Implementations MUST support field-level redaction at write time, declared in contracts.",
    ),
    Must(
        "9.4",
        "9.4",
        "Given the ledger alone, an implementation MUST be able to reconstruct "
        "session state, verdicts, and the compensation set.",
    ),
    Must("10.1", "10.1", "Rewind of a live session MUST first fence the session (no new steps)."),
    Must(
        "10.2",
        "10.2",
        "Declared `verification` blocks MUST be checked and recorded during rewind.",
    ),
    Must(
        "10.3.1",
        "10.3",
        "A rewind result MUST enumerate steps compensated, skipped, "
        "irreversible, and indeterminate.",
    ),
    Must(
        "10.3.2",
        "10.3",
        'Implementations MUST NOT report a session "fully rewound" unless every '
        "in-scope step was compensated with passing verification.",
    ),
    Must(
        "12.1",
        "12",
        "Approval, policy-relaxation, and contract-edit surfaces MUST NOT be "
        "exposed as tools to the protected agent.",
    ),
    Must(
        "12.3",
        "12",
        "Approval UIs MUST display the plan actually bound to the approval "
        "(`plan_id`), not a paraphrase.",
    ),
    Must("14.1", "14", "Unknown fields MUST be preserved in ledger events (evidence is tolerant)."),
    Must(
        "14.2",
        "14",
        "Unknown fields MUST be rejected in contracts and policies (authority is strict).",
    ),
]

MUST_BY_ID = {m.id: m for m in MUSTS}


def _iter_test_files(roots: list[str]) -> list[Path]:
    files = []
    for root in roots:
        base = REPO_ROOT / root
        if base.exists():
            files.extend(sorted(base.rglob("test_*.py")))
    return files


def _test_functions_with_docstring(source: str) -> list[tuple[str, str]]:
    """Return (function_name, docstring) for every top-level/class test function."""
    tree = ast.parse(source)
    out: list[tuple[str, str]] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith(
                "test_"
            ):
                doc = ast.get_docstring(child) or ""
                out.append((child.name, doc))
            if isinstance(child, ast.ClassDef):
                visit(child)

    visit(tree)
    return out


def scan_markers(roots: list[str] | None = None) -> dict[str, list[str]]:
    """Scan test files for @spec("X.Y") markers in test docstrings.

    Returns {must_id: ["path/to/file.py::test_name", ...]}.
    """
    roots = roots or TEST_ROOTS
    coverage: dict[str, list[str]] = {}
    for path in _iter_test_files(roots):
        source = path.read_text(encoding="utf-8")
        try:
            functions = _test_functions_with_docstring(source)
        except SyntaxError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for name, doc in functions:
            for match in _SPEC_MARKER_RE.finditer(doc):
                must_id = match.group(1)
                coverage.setdefault(must_id, []).append(f"{rel}::{name}")
    return coverage


def build_report(musts: list[Must], coverage: dict[str, list[str]]) -> tuple[str, list[Must]]:
    """Return (markdown_table, uncovered_musts)."""
    lines = [
        "# Traceability matrix",
        "",
        "Spec section -> normative MUST -> covering test(s). Generated by "
        "`scripts/traceability.py` from the hand-curated MUST list in that file "
        "cross-referenced against `@spec(\"X.Y\")` markers in `tests/` and "
        "`conformance/tests/`. Do not hand-edit; regenerate instead.",
        "",
        "| Spec § | MUST | Covering test(s) |",
        "|---|---|---|",
    ]
    uncovered: list[Must] = []
    for must in musts:
        tests = coverage.get(must.id, [])
        if not tests:
            uncovered.append(must)
            cell = "**MISSING**"
        else:
            cell = "<br>".join(f"`{t}`" for t in tests)
        lines.append(f"| {must.section} | {must.text} | {cell} |")
    lines.append("")
    return "\n".join(lines), uncovered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify coverage only; do not write docs/traceability.md",
    )
    args = parser.parse_args(argv)

    coverage = scan_markers()
    table, uncovered = build_report(MUSTS, coverage)

    if not args.check:
        out_path = REPO_ROOT / "docs" / "traceability.md"
        out_path.write_text(table + "\n", encoding="utf-8")

    if uncovered:
        sys.stderr.write(
            f"traceability FAILED: {len(uncovered)} MUST(s) with no covering test:\n"
        )
        for must in uncovered:
            sys.stderr.write(f"  - spec §{must.section} [{must.id}]: {must.text}\n")
        return 1

    print(f"traceability OK: {len(MUSTS)} MUSTs, all covered by at least one test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
