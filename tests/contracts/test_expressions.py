"""Unit + property tests for the Belay expression language (spec §4.3)."""

from __future__ import annotations

import pytest
from belay.contracts.expressions import evaluate, parse
from belay.errors import BelayError
from hypothesis import given
from hypothesis import strategies as st

SCOPE = {
    "args": {"id": 42, "path": "/tmp/x", "nested": {"a": {"b": 7}}},
    "result": {"id": 42, "status": "ok"},
    "context": {"session_id": "s_1", "step_seq": 3},
    "state": {"before": {"content": "old"}},
}


# --- literals & paths -------------------------------------------------


def test_parses_and_evaluates_args_path() -> None:
    assert evaluate(parse("$args.id"), SCOPE) == 42


def test_parses_and_evaluates_result_path() -> None:
    assert evaluate(parse("$result.status"), SCOPE) == "ok"


def test_parses_and_evaluates_context_path() -> None:
    assert evaluate(parse("$context.session_id"), SCOPE) == "s_1"


def test_parses_and_evaluates_state_path() -> None:
    assert evaluate(parse("$state.before.content"), SCOPE) == "old"


def test_supports_nested_path_access() -> None:
    assert evaluate(parse("$args.nested.a.b"), SCOPE) == 7


def test_missing_path_resolves_to_none() -> None:
    assert evaluate(parse("$args.nope"), SCOPE) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("42", 42),
        ("4.5", 4.5),
        ('"hi"', "hi"),
        ("'hi'", "hi"),
        ("true", True),
        ("false", False),
        ("null", None),
    ],
)
def test_literals(text: str, expected: object) -> None:
    assert evaluate(parse(text), SCOPE) == expected


# --- operators (every operator in §4.3 MUST be supported) -------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$args.id == 42", True),
        ("$args.id != 42", False),
        ("$args.id < 100", True),
        ("$args.id > 100", False),
        ("$result.status in $args.nested.a", False),
        ("true and true", True),
        ("true and false", False),
        ("false or true", True),
        ("not false", True),
        ("not (true and false)", True),
        ("coalesce($args.nope, 5)", 5),
        ("coalesce($args.id, 5)", 42),
    ],
)
def test_operators(text: str, expected: object) -> None:
    assert evaluate(parse(text), SCOPE) == expected


def test_in_operator_over_a_string_container() -> None:
    assert evaluate(parse('"ok" in $result.status'), SCOPE) is True


def test_precedence_and_binds_tighter_than_or() -> None:
    # false or (true and false) == false
    assert evaluate(parse("false or true and false"), SCOPE) is False


# --- rejection: anything outside the grammar --------------------------


@pytest.mark.parametrize(
    "text",
    [
        "__import__('os')",
        "$args.__class__",
        "$args.foo.__init__.__globals__",
        "print('hi')",
        "open('/etc/passwd')",
        "$args.id + 1",  # '+' is not in the grammar
        "os.system('rm -rf /')",
        "$args.id ==",  # incomplete
        "",
        "   ",
        "$unknownroot.x",
        "coalesce($args.id)",  # needs >= 2 args
    ],
)
def test_rejects_out_of_grammar_expressions(text: str) -> None:
    """@spec("4.3") — implementations MUST reject any construct outside the expression grammar."""
    with pytest.raises(BelayError) as exc_info:
        parse(text)
    assert exc_info.value.code == "expression_invalid"


def test_rejects_dunder_attribute_access_explicitly() -> None:
    with pytest.raises(BelayError) as exc_info:
        parse("$state.__class__")
    assert exc_info.value.code == "expression_invalid"


def test_rejects_function_calls_other_than_coalesce() -> None:
    with pytest.raises(BelayError) as exc_info:
        parse("eval($args.id)")
    assert exc_info.value.code == "expression_invalid"


def test_never_uses_eval_or_exec_module_globals() -> None:
    # Guards against a regression that reintroduces eval/exec.
    import belay.contracts.expressions as expr_mod

    src = expr_mod.__file__
    with open(src, encoding="utf-8") as f:
        text = f.read()
    assert "eval(" not in text
    assert "exec(" not in text


# --- property: only in-grammar tokens ever parse ----------------------

_SAFE_ATOMS = st.sampled_from(
    [
        "$args.id",
        "$result.status",
        "$context.session_id",
        "$state.before",
        "42",
        "4.5",
        '"hi"',
        "true",
        "false",
        "null",
    ]
)
_SAFE_OPS = st.sampled_from(["==", "!=", "<", ">", "and", "or"])


@given(a=_SAFE_ATOMS, op=_SAFE_OPS, b=_SAFE_ATOMS)
def test_property_in_grammar_combinations_always_parse_or_raise_expression_invalid(
    a: str, op: str, b: str
) -> None:
    text = f"{a} {op} {b}"
    try:
        expr = parse(text)
        # If it parsed, evaluating it must never execute arbitrary code — it
        # can only ever touch the closed scope mapping (type mismatches are
        # a legitimate expression_invalid, not a crash).
        evaluate(expr, SCOPE)
    except BelayError as exc:
        assert exc.code == "expression_invalid"


@given(st.text(alphabet="()[]{}<>=!&|_.$ \t\n\"'0123456789abcdefghijklmnopqrstuvwxyz", max_size=40))
def test_property_arbitrary_grammar_alphabet_strings_never_crash_the_parser(text: str) -> None:
    try:
        parse(text)
    except BelayError as exc:
        assert exc.code == "expression_invalid"
