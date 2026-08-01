# ADR 0024: Operator-configurable extra Bash allowlist (R1, fifth slice)

## Status

Accepted, implemented (opt-in, off by default).

## Context

Bash's remaining gap in R1 (extending it to a real `PolicyEngine` like the
MCP proxy has) was explicitly scoped out as a different kind of problem:
`belay/hooks/decision.py::classify_bash` classifies arbitrary shell text,
not a fixed tool name -- there is no stable identity to resolve a
`Contract` against the way `--contracts`/`--quota-max` do for
file-edit/MCP/quota slices. Assigning policy-evaluable "effects" to e.g.
`rm -rf $(find . -name "*.tmp")` needs either a real shell-command effect
classifier (research-level scope) or per-exact-string declarations (which
doesn't generalize) -- neither is a slice, both are separate projects.

What *is* a real, bounded slice: the built-in safe-read allowlist
(`_SAFE_READ_PATTERNS` -- `ls`, `cat`, `git status`/`diff`/`log`/`show`,
`grep`, `pytest`, `pwd`, `echo`, etc.) is hardcoded and identical for every
install. An operator whose project has its own genuinely safe, frequently
run commands (a lint/test/format command with no side effects, say) has
no way to add them without editing `belay`'s own source. This is not "Bash
gets governed like the MCP proxy" -- it is a narrower, honestly-scoped
improvement to the existing allowlist mechanism, and is documented as
such rather than conflated with the harder problem.

## Decision

`belay/hooks/decision.py::load_extra_allowlist(path)` parses a plain-text
file: one **literal** command prefix per line, blank lines and
`#`-comment lines ignored. Deliberately literal strings, never regex --
letting an operator author a regex here risks an accidental `.*`-shaped
hole opening in a security allowlist, a worse failure mode than the minor
inconvenience of listing a few literal variants by hand. An entry
matches the exact string, or that string followed by whitespace and
further arguments (`"npm run lint"` also matches `"npm run lint --fix"`).

`classify_bash(command, *, extra_allowlist=())` checks these entries only
*after* the built-in patterns, and — critically — only after the same
shell-metacharacter guard every command already passes through first.
This means an operator-supplied entry can never itself become a chaining/
redirection/substitution bypass: `"npm run lint; rm -rf /"` is rejected by
the metacharacter check before any allowlist (built-in or extra) is even
consulted, the same as it always was. Loading itself also refuses any
entry containing a metacharacter (fails loudly at load time — such an
entry could never match a real command anyway, so shipping it silently
would just be confusing dead configuration).

Configuration mirrors ADR 0021/0023's pattern exactly: `belay hooks
install --allowlist-extra <file>`, validated eagerly at install time
(parses and rejects invalid entries before anything is written),
persisted via a one-line pointer file
(`SupervisorIdentity.extra_allowlist_pointer_path`), loaded once by
`Supervisor.__init__` into `self._extra_allowlist: ExtraAllowlist` (`()`
— the default — is fully unchanged legacy behavior; a missing or invalid
pointed-to file falls back to `()` rather than crashing the supervisor or
denying every Bash command).

## Consequences

- An operator can extend the safe-read allowlist for their own project's
  genuinely safe commands, without a source change to `belay` itself.
- Strictly additive and one-directional: can only turn a PAUSE into an
  ALLOW for an entry the operator explicitly wrote, never weakens the
  metacharacter guard or the built-in patterns.
- Does **not** give Bash a `PolicyEngine`, effect declarations, or any of
  the MCP proxy's reversibility/blast-radius reasoning -- it is a
  configurable allowlist, nothing more. Extending Bash to real
  policy-evaluated governance remains explicitly out of scope and
  unaddressed by this slice.
- Known minor gap, inherited from ADR 0021/0023 and not fixed here
  either: `belay hooks uninstall` does not clear
  `extra_allowlist_pointer_path` (or `contracts_pointer_path`/
  `quota_config_path`) -- a subsequent `hooks install` without
  `--allowlist-extra` leaves a stale prior file in place until removed by
  hand.

## Testing

`tests/hooks/test_decision.py::TestExtraAllowlist` (11 cases: empty
default unchanged, exact match allows, trailing-arguments match, a
different-word suffix does not falsely match, shell-chaining still
pauses even with a configured entry, comment/blank-line parsing, metachar
entries rejected with a line number in the error, built-in patterns still
checked alongside extra ones). `tests/supervisor/test_extra_allowlist_loading.py`
(the pointer-file load path: absent, valid, missing target, invalid
content, empty). `tests/cli/test_hooks_lifecycle.py::TestHooksAllowlistExtra`
(a real CLI round trip through an actual spawned supervisor: a configured
entry allows, an unrelated command still pauses, no `--allowlist-extra`
is unchanged, an invalid entry or missing file is rejected at install
time with nothing written).
