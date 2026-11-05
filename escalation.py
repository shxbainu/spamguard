"""Punishment ladder: each new offense moves a user one step up, ending in a kick."""
from __future__ import annotations

import re
from dataclasses import dataclass

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MAX_TIMEOUT_SECONDS = 28 * 86400  # Discord's limit


@dataclass(frozen=True)
class Step:
    action: str  # "timeout" or "kick"
    seconds: int  # timeout length, or for a kick how long until they may rejoin

    def describe(self) -> str:
        if self.action == "kick":
            return f"kick ({format_duration(self.seconds)} before rejoining)"
        return f"{format_duration(self.seconds)} timeout"


def parse_duration(text: str) -> int:
    match = re.fullmatch(r"(\d+)\s*([smhd])", text.strip().lower())
    if not match:
        raise ValueError(f"Bad duration {text!r}; use e.g. 30s, 5m, 1h, 1d")
    return int(match.group(1)) * _UNITS[match.group(2)]


def format_duration(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def parse_ladder(text: str, kick_seconds: int) -> list[Step]:
    """'30s,2m,10m,1h,kick' -> the steps in order."""
    steps = []
    for item in (part.strip().lower() for part in text.split(",")):
        if not item:
            continue
        if item == "kick":
            steps.append(Step("kick", kick_seconds))
            continue
        seconds = parse_duration(item)
        if seconds > MAX_TIMEOUT_SECONDS:
            raise ValueError(f"Timeouts can be at most 28d, got {item!r}")
        steps.append(Step("timeout", seconds))
    if not steps:
        raise ValueError("PUNISHMENTS needs at least one step")
    return steps


def step_for(ladder: list[Step], offense: int) -> Step:
    """Offense 1 gets the first step; anything past the end repeats the last step."""
    return ladder[min(max(offense, 1), len(ladder)) - 1]
