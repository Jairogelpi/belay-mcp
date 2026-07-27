"use strict";

const { spawnSync } = require("node:child_process");
const { findPython } = require("./find-python.js");

const VERSION = require("../package.json").version.replace("-alpha.", "a");

function main() {
  const python = findPython();
  if (!python) {
    console.error(
      "belay-mcp: no Python 3.12+ interpreter found on PATH. " +
      "Install Python (https://python.org) and re-run `npm install`, " +
      "or `pip install belay-mcp==" + VERSION + "` yourself."
    );
    // Non-fatal: `npx belay-mcp` still gives a clear error at run time.
    return;
  }

  const result = spawnSync(
    python,
    ["-m", "pip", "install", "--quiet", "--upgrade", `belay-mcp==${VERSION}`],
    { stdio: "inherit" }
  );

  if (result.status !== 0) {
    console.error(
      `belay-mcp: "pip install belay-mcp==${VERSION}" failed (see above). ` +
      "Install it yourself: pip install belay-mcp==" + VERSION
    );
  }
}

main();
