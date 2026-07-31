# belay-mcp (npm wrapper)

Belay is a Python package; this npm package exists so MCP clients that only
know how to launch things via `npx` (or users without `pip` habits) can run
it with:

```
npx belay-mcp wrap ...
npx belay-mcp run --config belay.wrap.json
```

`npm install` runs a `postinstall` step that `pip install`s the matching
`belay-mcp` version from PyPI. A Python 3.12+ interpreter must already be on
`PATH` (`python`, `python3`, or on Windows `py`) -- this package does not
bundle or install Python itself.

Source, docs, and the actual implementation live in the main repo:
https://github.com/Jairogelpi/belay-mcp
