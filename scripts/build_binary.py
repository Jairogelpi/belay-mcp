"""Build a standalone `belay` binary (plan-v2 E19.5) -- no Python
installation required to run it, one file, works the same as the pip-
installed `belay` CLI (same `belay.cli.main:main` entry point PyPI's
console_script uses, so there is exactly one code path to keep working,
not a second copy-pasted one for the compiled build).

Usage:
    pip install -e ".[build]"
    python scripts/build_binary.py

Output: dist-bin/belay(.exe) -- a single-file, self-contained executable.
`--collect-all belay` (only `belay`, not `conformance` -- that's a
separate `belay-conformance` console script/entry point this binary
doesn't expose, bundling it just inflated the binary for no reason, an
early version of this script did exactly that) because this project's CLI
leans on lazy, function-body-local imports throughout (startup speed);
PyInstaller's static analysis follows literal `from x import y`
statements wherever they appear in a file it's already decided to
include, but `--collect-all` is the belt-and-suspenders way to make sure
every module under `belay` ends up in the bundle even if some import path
is only reachable through a code branch PyInstaller's graph walk doesn't
happen to trace on its own (e.g. a module imported only inside one CLI
subcommand's function body that PyInstaller's bytecode scan still needs
to actually open to see).
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    dist_path = REPO_ROOT / "dist-bin"
    work_path = REPO_ROOT / "build-pyinstaller"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        "belay",
        "--distpath",
        str(dist_path),
        "--workpath",
        str(work_path),
        "--specpath",
        str(work_path),
        "--clean",
        "--noconfirm",
        "--collect-all",
        "belay",
        str(REPO_ROOT / "belay" / "cli" / "main.py"),
    ]
    print(f"running: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        return result.returncode

    binary_name = "belay.exe" if platform.system() == "Windows" else "belay"
    binary = dist_path / binary_name
    if not binary.is_file():
        print(f"error: PyInstaller reported success but {binary} was not created", file=sys.stderr)
        return 1
    print(f"built: {binary} ({binary.stat().st_size:,} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
