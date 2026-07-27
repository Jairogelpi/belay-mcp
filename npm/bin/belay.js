#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const path = require("node:path");
const { findPython } = require("../scripts/find-python.js");

const python = findPython();
if (!python) {
  console.error(
    "belay-mcp: no Python 3.12+ interpreter found on PATH. " +
    "Install Python (https://python.org), then `pip install belay-mcp`."
  );
  process.exit(1);
}

// Prefer the real `belay` console-script if pip put it on PATH (faster
// startup, exact entry point) -- fall back to `python -m belay.cli.main`
// otherwise (works even if pip's script dir isn't on PATH).
const direct = spawnSync("belay", process.argv.slice(2), { stdio: "inherit" });
if (direct.error === undefined || direct.error.code !== "ENOENT") {
  process.exit(direct.status === null ? 1 : direct.status);
}

const result = spawnSync(
  python,
  ["-m", "belay.cli.main", ...process.argv.slice(2)],
  { stdio: "inherit" }
);

if (result.error) {
  console.error(
    "belay-mcp: failed to launch (" + result.error.message + "). " +
    "Try: pip install belay-mcp"
  );
  process.exit(1);
}
process.exit(result.status === null ? 1 : result.status);
