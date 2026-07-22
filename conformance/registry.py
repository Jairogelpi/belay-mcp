"""Resolve a `--target` CLI string to a `ConformanceTarget` instance.

`belay` is the built-in alias for the reference implementation in this
repo. Any other implementation names itself with a dotted `module:Class`
path -- no plugin registry needed for "runnable against any implementation".
"""

from __future__ import annotations

import importlib

from conformance.target import ConformanceTarget

_BUILTIN = {
    "belay": "conformance.targets.belay_target:BelayConformanceTarget",
}


def resolve_target(name: str) -> ConformanceTarget:
    dotted = _BUILTIN.get(name, name)
    if ":" not in dotted:
        raise ValueError(
            f"unknown target {name!r}: expected a built-in alias ({', '.join(_BUILTIN)}) "
            "or a 'module:ClassName' path"
        )
    module_name, class_name = dotted.split(":", 1)
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    target: ConformanceTarget = cls()
    return target
