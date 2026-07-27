"use strict";

const { spawnSync } = require("node:child_process");

// Windows ships `py`; most Unix systems only have `python3`. Try the likely
// names in order and keep the first one that actually runs and is >= 3.12
// (belay-mcp's `requires-python`).
const CANDIDATES = process.platform === "win32"
  ? ["py", "python", "python3"]
  : ["python3", "python"];

function findPython() {
  for (const candidate of CANDIDATES) {
    const probe = spawnSync(candidate, ["--version"], { encoding: "utf8" });
    if (probe.status === 0) {
      return candidate;
    }
  }
  return null;
}

module.exports = { findPython };
