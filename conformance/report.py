"""The report `belay-conformance run` prints (plan.md E8 exit criterion)."""

from __future__ import annotations

from dataclasses import dataclass

_LEVEL_MARKERS: dict[int, tuple[str, ...]] = {
    1: ("l1",),
    2: ("l1", "l2"),
    3: ("l1", "l2", "l3"),
}


def markers_for_level(level: int) -> tuple[str, ...]:
    """Levels are cumulative (spec §13): L3 also runs every L1/L2 scenario."""
    if level not in _LEVEL_MARKERS:
        raise ValueError(f"level must be 1, 2, or 3, got {level!r}")
    return _LEVEL_MARKERS[level]


@dataclass(frozen=True)
class ConformanceReport:
    target: str
    level: int
    exit_code: int

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def render(self) -> str:
        verdict = f"L{self.level} PASSED" if self.passed else f"L{self.level} FAILED"
        return f"belay-conformance: target={self.target} -> {verdict}"
