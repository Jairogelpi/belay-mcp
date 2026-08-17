"""Wheel-visible copies of Belay's own verified packs (E22).

`packs/` at the repo root (`packs/filesystem/`, `packs/git/`,
`packs/claude-code-native/`) is the canonical, developer-facing source of
truth -- it is not shipped inside the `belay` wheel (only `examples/` is,
via `pyproject.toml`'s `force-include`). `belay connect`'s zero-config
Filesystem flow needs its pinned pack's `pack.yaml`/`contracts.yaml` to be
readable from a real, installed (non-editable) `pip install belay-mcp`
with no repo checkout present at all, so E22 ships one pack -- Filesystem
only -- as actual package data under this directory, resolved through
`importlib.resources` (see `belay/bundled_packs.py`), and CI's wheel-smoke
step (Task 8) asserts the packaged bytes are byte-identical to the
canonical `packs/filesystem/` files rather than trusting a copy-paste to
stay in sync by hand.
"""

from __future__ import annotations
