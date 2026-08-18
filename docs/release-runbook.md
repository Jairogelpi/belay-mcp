# Release runbook (E23)

Operator procedure for cutting a Belay GitHub prerelease. This is a human
procedure with exact commands, not automation — `scripts/
release_preflight.py` (E23 Task 1) and `.github/workflows/release.yaml`
(E23 Task 2) do the actual validation/building; this document is the
order to run them in and what each result means.

Every command below uses `py -3.13` and assumes a checkout of this repo
with `gh` authenticated against an account with push access to
`Jairogelpi/belay-mcp` (`gh auth status` to check;
`gh auth switch --hostname github.com --user <account>` if more than one
account is logged in).

**Nothing in this document moves, recreates, or force-updates a tag once
pushed.** That constraint is the entire point of the "recoverable by
immutable tag" design (`docs/superpowers/plans/
2026-08-12-e23-prerelease-governance.md`) — see "Failure recovery" below.

## 1. Pre-tag gate

Run the composed check first — it runs everything below in one shot and
fails on the first problem it finds:

```bash
py -3.13 scripts/release_preflight.py prepare --tag v0.2.0a1 --repo Jairogelpi/belay-mcp
```

`prepare` requires `origin/main` to already have every one of E23's nine
required checks green on its own tip commit — it resolves that commit
itself, it does not take a SHA as input. If it fails, the individual
checks it composes are useful to run one at a time to see exactly which
one is the problem:

**Clean worktree:**

```bash
git status --porcelain   # must print nothing
```

**Local `main` matches `origin/main` exactly (no unpushed or unpulled commits):**

```bash
git fetch origin main
git rev-parse main
git rev-parse origin/main
# both SHAs must be identical
```

**Python (`pyproject.toml`) and npm (`npm/package.json`) versions agree
with the tag you're about to cut** (PEP 440 `0.2.0a1` <-> npm semver
`0.2.0-alpha.1` for tag `v0.2.0a1`):

```bash
grep '^version' pyproject.toml
grep '"version"' npm/package.json
```

`release_preflight.py`'s `check_version_consistency` (used internally by
`prepare`) is the authoritative check — the two `grep`s above are just a
fast human sanity read.

**The tag does not already exist, locally or on the remote** (a genuinely
new tag, not a reuse or an accidental duplicate):

```bash
git tag --list v0.2.0a1                                  # must print nothing
git ls-remote --tags origin refs/tags/v0.2.0a1            # must print nothing
```

**Every one of E23's nine required checks is green on the exact candidate
commit** (`origin/main`'s own tip, i.e. what `check_local_matches_remote_main`
above just confirmed local `main` equals):

```bash
SHA=$(git rev-parse origin/main)
py -3.13 scripts/release_preflight.py required-checks --repo Jairogelpi/belay-mcp --sha "$SHA"
```

The nine required check-run names (exact strings, `scripts/
release_preflight.py::REQUIRED_CHECK_NAMES`):
`test (3.12)`, `test (3.13)`, `wheel-smoke`,
`cross-platform-clean-room (ubuntu-latest)`,
`cross-platform-clean-room (macos-latest)`,
`cross-platform-clean-room (windows-latest)`,
`build-binaries (ubuntu-latest)`, `build-binaries (macos-latest)`,
`build-binaries (windows-latest)`.

**The PyPI publish gate is disabled or absent** (the honest default for
`v0.2.0a1` — publishing was never independently configured, see
"Release notes" below):

```bash
gh variable list --repo Jairogelpi/belay-mcp
# PYPI_PUBLISH_ENABLED should be absent, or present and not "true"
```

If it prints `PYPI_PUBLISH_ENABLED  true`, PyPI publishing WILL run as
part of this release — confirm that is actually intended (real PyPI
Trusted Publishing configured on the PyPI project side, see `.github/
workflows/release.yaml`'s own top-of-file comment) before proceeding.

## 2. Cut the tag

Only after every check in §1 passes:

```bash
git tag -a v0.2.0a1 -m "v0.2.0a1"
git push origin v0.2.0a1
```

An annotated tag (`-a`), not lightweight — `resolve_tag_sha` (Task 1)
handles both, but an annotated tag records who cut it and when, which is
worth having for a release.

Pushing the tag triggers `.github/workflows/release.yaml`'s `push: tags:`
trigger automatically — no further manual step needed for a normal,
successful run.

## 3. Monitor the release workflow

```bash
gh run list --repo Jairogelpi/belay-mcp --workflow release.yaml --limit 1
gh run watch --repo Jairogelpi/belay-mcp <run-id>
```

The `resolve` job's own log line (`cat resolved.json`) states the exact
tag and SHA every other job in that run built from — compare it to
`git rev-parse v0.2.0a1^{}` (peel the tag to its commit) to confirm the
workflow resolved the *same* commit you tagged.

## 4. Verify the published release

```bash
mkdir -p /tmp/release-verify
gh release download v0.2.0a1 --repo Jairogelpi/belay-mcp --dir /tmp/release-verify
py -3.13 scripts/release_preflight.py verify-artifacts --tag v0.2.0a1 --directory /tmp/release-verify
gh release view v0.2.0a1 --repo Jairogelpi/belay-mcp --json isPrerelease,tagName,targetCommitish
```

Confirm: `isPrerelease` is `true` (alpha tag), `tagName` is `v0.2.0a1`,
`targetCommitish` matches the SHA from §3, and `verify-artifacts` reports
all five classes (sdist, wheel, Linux/macOS/Windows binaries) present,
non-empty, and version-matched — exits 0.

## Failure recovery

If the release workflow run fails partway (a flaky runner, a bug in the
workflow itself, a transient `gh`/PyPI outage) **the tag is never moved,
recreated, or force-updated.** Recovery only ever touches the *workflow*,
never the tag:

1. **Mark the release incomplete**, so nobody mistakes a partial run for
   a finished one, while the fix is in progress:

   ```bash
   gh release edit v0.2.0a1 --repo Jairogelpi/belay-mcp \
     --notes "INCOMPLETE -- release workflow run <run-id> failed at <job>. Fix in progress, see <PR URL>."
   ```

2. **Repair the workflow through a normal PR to `main`** — branch, fix
   `.github/workflows/release.yaml` (or whatever the actual root cause
   was), open a PR, get it merged like any other change. Never edit the
   workflow file directly on the tag's commit; the tag's tree is frozen.

3. **Re-dispatch the repaired workflow from `main`, against the SAME
   tag:**

   ```bash
   gh workflow run release.yaml --repo Jairogelpi/belay-mcp --ref main -f tag=v0.2.0a1
   ```

   The `resolve` job checks out `main` (the repaired workflow file) but
   resolves `v0.2.0a1` to its own commit exactly as the original tag push
   did — the SAME candidate SHA gets rebuilt from scratch, not a moving
   target.

4. **Compare the resolved SHA to the original tag push's resolved SHA**
   (they must be identical — the whole point of resolving from the tag,
   never from `GITHUB_SHA`/the triggering ref, is that this is guaranteed
   by construction, but confirm it in the new run's `resolve` job log
   regardless):

   ```bash
   py -3.13 scripts/release_preflight.py resolve-tag --tag v0.2.0a1
   ```

5. **Re-run the full five-class artifact inventory verification** (§4,
   §4 above) against the recovered run's assets before considering the
   release complete, and update the release notes to remove the
   "INCOMPLETE" marker from step 1.

## GitHub settings

Task 7/Task 9 of the E23 plan — done only after an immutable, verified
prerelease exists (§4 above passed). Not part of Tasks 1-5; documented
here so the same runbook covers the whole rollout in order.

**Enable private vulnerability reporting:**

```bash
gh api --method PUT repos/Jairogelpi/belay-mcp/private-vulnerability-reporting
```

**Read it back** (must report the feature enabled):

```bash
gh api repos/Jairogelpi/belay-mcp/private-vulnerability-reporting
```

**Protect `main`** — a ruleset requiring a pull request, zero required
human approvals (this is a single-maintainer project; a second-reviewer
requirement would just block the maintainer on themselves), an
administrator bypass (so the maintainer is never physically locked out),
and exactly E23's nine required status-check contexts (same list as §1
above). Example request body (`ruleset.json`, kept outside the repo —
never commit a file like this with real IDs baked in casually, though
nothing here is secret):

```json
{
  "name": "main-protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/main"], "exclude": [] } },
  "rules": [
    { "type": "pull_request", "parameters": { "required_approving_review_count": 0 } },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "test (3.12)" },
          { "context": "test (3.13)" },
          { "context": "wheel-smoke" },
          { "context": "cross-platform-clean-room (ubuntu-latest)" },
          { "context": "cross-platform-clean-room (macos-latest)" },
          { "context": "cross-platform-clean-room (windows-latest)" },
          { "context": "build-binaries (ubuntu-latest)" },
          { "context": "build-binaries (macos-latest)" },
          { "context": "build-binaries (windows-latest)" }
        ]
      }
    }
  ],
  "bypass_actors": [
    { "actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "always" }
  ]
}
```

```bash
gh api --method POST repos/Jairogelpi/belay-mcp/rulesets --input ruleset.json
```

**Read it back** (verify every context, the pull-request rule, the
zero-approval setting, and the bypass actor are all exactly what was
requested — never assume the POST body was applied byte-for-byte):

```bash
gh api repos/Jairogelpi/belay-mcp/rulesets
gh api repos/Jairogelpi/belay-mcp/rulesets/<id>
```

## Release notes

### `v0.1.0` (historical, already tagged and immutable)

> `v0.1.0` is a historical, incomplete release candidate: it contains
> package version `0.1.0.dev0` (not `0.1.0`), its PyPI Trusted Publishing
> workflow failed because Trusted Publishing was never configured on the
> PyPI side (so it did not publish to PyPI), and it did not meet the
> historical global Definition of Done — 90% branch coverage and a
> clean-clone test run under 60 seconds. See
> [ADR 0027](adr/0027-e21-release-truth.md) for the full record. This tag
> is not moved, recreated, or force-updated to change that history — this
> note only documents it accurately after the fact.

### `v0.2.0a1` (planned next GitHub prerelease)

> `v0.2.0a1` is a **prerelease** (GitHub "prerelease" flag set, matching
> its PEP 440 alpha version): source distribution + wheel + standalone
> Linux/macOS/Windows binaries, all built and smoke-tested from the exact
> immutable tagged commit (`.github/workflows/release.yaml`, E23).
>
> **Scope:** zero-config `belay connect`/`belay disconnect` (E22) covers
> exactly one bundled, pinned upstream — the official Filesystem MCP
> server (`@modelcontextprotocol/server-filesystem`). No other server is
> wired into zero-config connect yet.
>
> **Known limitation:** Codex CLI only gets MCP-tool-call protection
> through this connection — there is no Codex-side native-tool hook
> equivalent to Claude Code's `PreToolUse`/`PostToolUse` gate (`belay
> hooks install`, E18, Claude Code only).
>
> **PyPI:** not published unless the `PYPI_PUBLISH_ENABLED` repository
> variable was independently set to `true` before this tag was pushed
> (see §1 above) *and* PyPI Trusted Publishing was independently
> configured on the `belay-mcp` PyPI project by a human with PyPI account
> access — neither of those is something this workflow or an agent can
> set up unattended. Check the `publish-pypi` job's own status in the
> release workflow run before assuming either happened.
