# Belay one-line installer (Windows PowerShell):
#
#   irm https://raw.githubusercontent.com/Jairogelpi/belay-mcp/main/scripts/install.ps1 | iex
#
# belay-mcp isn't published to PyPI yet (see README "Release status"), so
# `pip install belay-mcp` doesn't work for anyone but the maintainer. This
# installs the exact same package straight from GitHub instead -- same
# result, no registry required. Once it's on PyPI this script (and its
# $BelayRepo/$BelayRef default below) switches over; nothing else about
# the UX changes.
#
# Prefers `pipx` (isolated venv, `belay` on PATH -- the standard way to
# install a Python CLI tool without polluting any project's environment).
# Falls back to `pip install --user` if `pipx` isn't present, since
# requiring a second tool just to install the first is exactly the kind of
# friction this script exists to remove.
#
# Env overrides (mainly for testing this script itself, or installing a
# fork/branch): $env:BELAY_REPO (default Jairogelpi/belay-mcp), $env:BELAY_REF
# (default main).

$ErrorActionPreference = "Stop"

$BelayRepo = if ($env:BELAY_REPO) { $env:BELAY_REPO } else { "Jairogelpi/belay-mcp" }
$BelayRef = if ($env:BELAY_REF) { $env:BELAY_REF } else { "main" }
$BelaySource = "git+https://github.com/$BelayRepo.git@$BelayRef"

function Write-Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "!! $msg" -ForegroundColor Yellow }
function Die($msg) { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }

# belay-mcp requires Python >= 3.12 (pyproject.toml `requires-python`). Try
# the interpreter names most systems actually have, in order, and keep the
# first one new enough -- rather than trusting whichever `python` happens
# to be first on PATH.
# PowerShell 5.1's `$arr[1..($arr.Length-1)]` does NOT return an empty array
# when $arr has only one element (it degenerates to $arr[0] instead of
# $arr[] -- confirmed empirically, not documented behavior to rely on
# blindly) -- hence the explicit length check rather than the range-slice
# idiom alone.
function Split-PythonCommand($cmd) {
    $parts = $cmd.Split(" ")
    $exeArgs = if ($parts.Length -gt 1) { $parts[1..($parts.Length - 1)] } else { @() }
    return @{ Exe = $parts[0]; Args = $exeArgs }
}

function Find-Python {
    foreach ($candidate in @("py -3.13", "py -3.12", "py", "python3", "python")) {
        $split = Split-PythonCommand $candidate
        $cmd = Get-Command $split.Exe -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            & $split.Exe @($split.Args) -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch {}
    }
    return $null
}

$PythonCmd = Find-Python
if (-not $PythonCmd) {
    Die "no Python 3.12+ interpreter found on PATH. Install one from https://python.org, then re-run this script."
}
$PythonSplit = Split-PythonCommand $PythonCmd
$PythonExe = $PythonSplit.Exe
$PythonArgs = $PythonSplit.Args
$versionOutput = & $PythonExe @($PythonArgs) --version 2>&1
Write-Info "using $versionOutput ($PythonCmd)"

$pipx = Get-Command pipx -ErrorAction SilentlyContinue
if ($pipx) {
    Write-Info "installing belay-mcp with pipx from $BelaySource"
    pipx install --force $BelaySource --python $PythonExe
} else {
    Write-Warn "pipx not found -- falling back to 'pip install --user' (pipx is recommended: https://pipx.pypa.io)"
    Write-Info "installing belay-mcp with pip --user from $BelaySource"
    & $PythonExe @($PythonArgs) -m pip install --user --upgrade $BelaySource
}

$belayCmd = Get-Command belay -ErrorAction SilentlyContinue
if ($belayCmd) {
    Write-Info "installed: belay on PATH at $($belayCmd.Source)"
    $RunBelay = "belay"
} else {
    $RunBelay = "$PythonCmd -m belay.cli.main"
    Write-Warn "'belay' isn't on PATH yet -- for pip --user installs, add your Python Scripts dir to PATH"
    Write-Warn "  (run '$PythonCmd -m site --user-site' to find it), or run belay as: $RunBelay"
}

Write-Host ""
Write-Host "Belay installed. Next, inside the project whose MCP server you want to guard:"
Write-Host ""
Write-Host "  cd your-project"
Write-Host "  $RunBelay bootstrap ./your-server-dir --client all"
Write-Host ""
Write-Host "That drafts contracts, wraps the server, registers Belay with every MCP"
Write-Host "client it finds (Claude Desktop/Code, Cursor, Codex, OpenCode), and adds a"
Write-Host "standing instruction to AGENTS.md/CLAUDE.md so agents use it by default."
Write-Host "See https://github.com/$BelayRepo#readme for the full quickstart."
