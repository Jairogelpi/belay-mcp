"""The Belay expression language (spec §4.3): parse() and evaluate().

Grammar (normative, spec §4.3):

    $args.<path> | $result.<path> | $context.<key> | $state.<path>
    literals: strings, numbers, true, false, null
    operators: == != < > in and or not
    coalesce(a, b)

This is a hand-written recursive-descent parser and tree-walking evaluator —
a deliberate, closed grammar. No `eval`/`exec`, no Python `ast` module, no
function calls other than the one blessed builtin (`coalesce`), no attribute
access beyond dotted-path traversal of plain mapping data. Anything outside
the grammar — including `__import__`, dunder attribute segments, and calls to
anything other than `coalesce` — is rejected with `expression_invalid`. This
is a security boundary: contracts are data, never code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from belay.errors import BelayError

_ROOTS = ("args", "result", "context", "state")

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<num>\d+\.\d+|\d+)
      | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
      | (?P<path>\$(?:args|result|context|state)(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<op>==|!=|<=|>=|<|>|\(|\)|,)
    )
    """,
    re.VERBOSE,
)

_KEYWORDS = {"and", "or", "not", "in", "true", "false", "null", "coalesce"}


@dataclass(frozen=True)
class Literal:
    value: Any


@dataclass(frozen=True)
class PathRef:
    root: str
    path: tuple[str, ...]


@dataclass(frozen=True)
class Coalesce:
    args: tuple[Expr, ...]


@dataclass(frozen=True)
class UnaryNot:
    operand: Expr


@dataclass(frozen=True)
class BinOp:
    op: str
    left: Expr
    right: Expr


Expr = Literal | PathRef | Coalesce | UnaryNot | BinOp

_COMPARISON_OPS = {"==", "!=", "<", ">", "<=", ">=", "in"}


class _Token:
    __slots__ = ("kind", "text")

    def __init__(self, kind: str, text: str) -> None:
        self.kind = kind
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Token({self.kind!r}, {self.text!r})"


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if m is None or m.end() == pos:
            if text[pos:].strip() == "":
                break
            raise BelayError(
                "expression_invalid",
                {"reason": "unrecognized token", "at": pos, "text": text},
            )
        pos = m.end()
        kind = m.lastgroup
        assert kind is not None
        value = m.group(kind)
        if kind == "ident" and value not in _KEYWORDS:
            raise BelayError(
                "expression_invalid",
                {"reason": f"unknown identifier {value!r}", "text": text},
            )
        tokens.append(_Token(kind, value))
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token], text: str) -> None:
        self._tokens = tokens
        self._pos = 0
        self._text = text

    def _peek(self) -> _Token | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _advance(self) -> _Token:
        tok = self._peek()
        if tok is None:
            self._fail("unexpected end of expression")
        self._pos += 1
        return tok  # type: ignore[return-value]

    def _fail(self, reason: str) -> None:
        raise BelayError("expression_invalid", {"reason": reason, "text": self._text})

    def _expect_op(self, text: str) -> None:
        tok = self._peek()
        if tok is None or tok.kind != "op" or tok.text != text:
            self._fail(f"expected {text!r}")
        self._pos += 1

    def parse(self) -> Expr:
        expr = self._parse_or()
        if self._peek() is not None:
            self._fail("trailing tokens after valid expression")
        return expr

    def _parse_or(self) -> Expr:
        left = self._parse_and()
        while (tok := self._peek()) is not None and tok.kind == "ident" and tok.text == "or":
            self._advance()
            right = self._parse_and()
            left = BinOp("or", left, right)
        return left

    def _parse_and(self) -> Expr:
        left = self._parse_not()
        while (tok := self._peek()) is not None and tok.kind == "ident" and tok.text == "and":
            self._advance()
            right = self._parse_not()
            left = BinOp("and", left, right)
        return left

    def _parse_not(self) -> Expr:
        tok = self._peek()
        if tok is not None and tok.kind == "ident" and tok.text == "not":
            self._advance()
            return UnaryNot(self._parse_not())
        return self._parse_comparison()

    def _parse_comparison(self) -> Expr:
        left = self._parse_primary()
        tok = self._peek()
        op: str | None = None
        if tok is not None:
            if tok.kind == "op" and tok.text in _COMPARISON_OPS:
                op = tok.text
            elif tok.kind == "ident" and tok.text == "in":
                op = "in"
        if op is not None:
            self._advance()
            right = self._parse_primary()
            return BinOp(op, left, right)
        return left

    def _parse_primary(self) -> Expr:
        tok = self._peek()
        if tok is None:
            self._fail("unexpected end of expression")
            raise AssertionError  # unreachable, keeps mypy happy
        if tok.kind == "op" and tok.text == "(":
            self._advance()
            expr = self._parse_or()
            self._expect_op(")")
            return expr
        if tok.kind == "num":
            self._advance()
            value: float | int = float(tok.text) if "." in tok.text else int(tok.text)
            return Literal(value)
        if tok.kind == "str":
            self._advance()
            return Literal(_unquote(tok.text))
        if tok.kind == "path":
            self._advance()
            return _parse_path(tok.text, self._text)
        if tok.kind == "ident":
            if tok.text == "true":
                self._advance()
                return Literal(True)
            if tok.text == "false":
                self._advance()
                return Literal(False)
            if tok.text == "null":
                self._advance()
                return Literal(None)
            if tok.text == "coalesce":
                self._advance()
                return self._parse_coalesce()
            self._fail(f"unexpected keyword {tok.text!r} in this position")
        self._fail(f"unexpected token {tok.text!r}")
        raise AssertionError  # unreachable

    def _parse_coalesce(self) -> Coalesce:
        self._expect_op("(")
        args = [self._parse_or()]
        while (tok := self._peek()) is not None and tok.kind == "op" and tok.text == ",":
            self._advance()
            args.append(self._parse_or())
        self._expect_op(")")
        if len(args) < 2:
            self._fail("coalesce() requires at least 2 arguments")
        return Coalesce(tuple(args))


def _unquote(literal: str) -> str:
    body = literal[1:-1]
    return body.replace("\\" + literal[0], literal[0]).replace("\\\\", "\\")


def _parse_path(token_text: str, full_text: str) -> PathRef:
    parts = token_text[1:].split(".")
    root, segments = parts[0], tuple(parts[1:])
    if root not in _ROOTS:
        raise BelayError(
            "expression_invalid", {"reason": f"unknown root {root!r}", "text": full_text}
        )
    for seg in segments:
        if seg.startswith("__") or seg.endswith("__"):
            raise BelayError(
                "expression_invalid",
                {"reason": f"dunder attribute access is forbidden: {seg!r}", "text": full_text},
            )
    return PathRef(root, segments)


def parse(text: str) -> Expr:
    """Parse `text` into an `Expr`, rejecting anything outside the grammar.

    Raises `BelayError(code="expression_invalid")` for malformed input,
    unknown identifiers, dunder attribute segments, or any construct not in
    spec §4.3's grammar (e.g. function calls other than `coalesce`).
    """
    if not isinstance(text, str) or text.strip() == "":
        raise BelayError("expression_invalid", {"reason": "empty expression"})
    tokens = _tokenize(text)
    if not tokens:
        raise BelayError("expression_invalid", {"reason": "empty expression"})
    return _Parser(tokens, text).parse()


Scope = dict[str, Any]


def _resolve_path(root_value: Any, segments: tuple[str, ...]) -> Any:
    current = root_value
    for seg in segments:
        if isinstance(current, dict):
            current = current.get(seg)
        else:
            return None
    return current


def evaluate(expr: Expr, scope: Scope) -> Any:
    """Evaluate `expr` against `scope` (`{args, result, context, state}`).

    Missing path segments resolve to `None` (use `coalesce` for defaults).
    Purely a tree-walk over data — no code execution of any kind.
    """
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, PathRef):
        root_value = scope.get(expr.root)
        return _resolve_path(root_value, expr.path)
    if isinstance(expr, Coalesce):
        for arg in expr.args:
            value = evaluate(arg, scope)
            if value is not None:
                return value
        return None
    if isinstance(expr, UnaryNot):
        return not evaluate(expr.operand, scope)
    if isinstance(expr, BinOp):
        left = evaluate(expr.left, scope)
        if expr.op == "and":
            return bool(left) and bool(evaluate(expr.right, scope))
        if expr.op == "or":
            return bool(left) or bool(evaluate(expr.right, scope))
        right = evaluate(expr.right, scope)
        if expr.op == "==":
            return left == right
        if expr.op == "!=":
            return left != right
        if expr.op in ("<", ">", "<=", ">="):
            try:
                if expr.op == "<":
                    return left < right
                if expr.op == ">":
                    return left > right
                if expr.op == "<=":
                    return left <= right
                return left >= right
            except TypeError as exc:
                raise BelayError(
                    "expression_invalid",
                    {"reason": f"cannot compare {left!r} {expr.op} {right!r}"},
                ) from exc
        if expr.op == "in":
            try:
                return left in right
            except TypeError as exc:
                raise BelayError(
                    "expression_invalid",
                    {"reason": f"right side of 'in' is not a container: {right!r}"},
                ) from exc
        raise AssertionError(f"unreachable operator {expr.op!r}")
    raise AssertionError(f"unreachable expr node {expr!r}")
