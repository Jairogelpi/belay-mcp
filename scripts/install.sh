#!/bin/sh
# Belay one-line installer (macOS/Linux/WSL):
#
#   curl -fsSL https://raw.githubusercontent.com/Jairogelpi/belay-mcp/main/scripts/install.sh | sh
#
# belay-mcp isn't published to PyPI yet (see README "Release status"), so
# `pip install belay-mcp` doesn't work for anyone but the maintainer. This
# installs the exact same package straight from GitHub instead -- same
# result, no registry required. Once it's on PyPI this script (and its
# BELAY_SOURCE default below) switches over; nothing else about the UX
# changes.
#
# Prefers `pipx` (isolated venv, `belay` on PATH, the standard way to
# install a Python CLI tool without polluting any project's environment --
# what `pipx`'s own docs recommend and what tools like `black`/`ruff` point
# users to). Falls back to `pip install --user` if `pipx` isn't present,
# since requiring a second tool just to install the first is exactly the
# kind of friction this script exists to remove.
#
# Env overrides (mainly for testing this script itself, or installing a
# fork/branch): BELAY_REPO (default Jairogelpi/belay-mcp), BELAY_REF
# (default main).

set -eu

BELAY_REPO="${BELAY_REPO:-Jairogelpi/belay-mcp}"
BELAY_REF="${BELAY_REF:-main}"
BELAY_SOURCE="git+https://github.com/${BELAY_REPO}.git@${BELAY_REF}"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
warn()  { printf '\033[1;33m!!\033[0m %s\n' "$1" >&2; }
die()   { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; exit 1; }

# belay-mcp requires Python >= 3.12 (pyproject.toml `requires-python`).
# Try the interpreter names most systems actually have, in order, and keep
# the first one new enough -- rather than trusting whichever `python3`
# happens to be first on PATH.
find_python() {
  for candidate in python3.13 python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON="$(find_python)" || die "no Python 3.12+ interpreter found on PATH. Install one from https://python.org, then re-run this script."
info "using $($PYTHON --version 2>&1) ($PYTHON)"

if command -v pipx >/dev/null 2>&1; then
  info "installing belay-mcp with pipx from ${BELAY_SOURCE}"
  pipx install --force "$BELAY_SOURCE" --python "$PYTHON"
else
  warn "pipx not found -- falling back to 'pip install --user' (pipx is recommended: https://pipx.pypa.io)"
  info "installing belay-mcp with pip --user from ${BELAY_SOURCE}"
  "$PYTHON" -m pip install --user --upgrade "$BELAY_SOURCE"
fi

if command -v belay >/dev/null 2>&1; then
  info "installed: $(belay --help >/dev/null 2>&1 && echo OK)"
  BELAY_CMD=belay
else
  BELAY_CMD="$PYTHON -m belay.cli.main"
  warn "'belay' isn't on PATH yet -- for pip --user installs, add your user script dir to PATH"
  warn "  (usually \$(${PYTHON} -m site --user-base)/bin), or run belay as: $BELAY_CMD"
fi

cat <<EOF

Belay installed. Next, inside the project whose MCP server you want to
guard:

  cd your-project
  $BELAY_CMD bootstrap ./your-server-dir --client all

That drafts contracts, wraps the server, registers Belay with every MCP
client it finds (Claude Desktop/Code, Cursor, Codex, OpenCode), and adds a
standing instruction to AGENTS.md/CLAUDE.md so agents use it by default.
See https://github.com/${BELAY_REPO}#readme for the full quickstart.
EOF
