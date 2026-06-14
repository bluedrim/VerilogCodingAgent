from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .models import SignalActivity


TIME_FACTORS_TO_NS = {
    "fs": 1e-6,
    "ps": 1e-3,
    "ns": 1.0,
    "us": 1e3,
    "ms": 1e6,
    "s": 1e9,
}

KEYWORD_WEIGHTS = {
    "valid": 4,
    "ready": 4,
    "state": 4,
    "error": 4,
    "fail": 4,
    "done": 3,
    "last": 3,
    "reset": 3,
    "rst": 3,
    "enable": 2,
    "en": 2,
    "data": 2,
    "addr": 2,
    "count": 2,
    "cnt": 2,
}

TIMESCALE_RE = re.compile(r"([0-9]+)\s*(fs|ps|ns|us|ms|s)", re.IGNORECASE)


@dataclass
class VcdSymbol:
    id_code: str
    name: str
    width: int


def _timescale_to_ns(line: str) -> Optional[float]:
    match = TIMESCALE_RE.search(line)
    if not match:
        return None
    return int(match.group(1)) * TIME_FACTORS_TO_NS[match.group(2).lower()]


def _contains_xz(value: str) -> bool:
    return "x" in value.lower() or "z" in value.lower()


def _record_activity(
    activities: Dict[str, SignalActivity], symbol: VcdSymbol, time_ns: float, value: str
) -> None:
    activity = activities.setdefault(symbol.name, SignalActivity(name=symbol.name, width=symbol.width))
    activity.changes += 1
    if activity.first_change_ns is None:
        activity.first_change_ns = time_ns
        activity.first_value = value
    activity.last_change_ns = time_ns
    activity.last_value = value
    if len(activity.values_seen) < 16:
        activity.values_seen.add(value)
    if _contains_xz(value):
        activity.xz_events += 1
        if activity.first_xz_ns is None:
            activity.first_xz_ns = time_ns


def _score_activity(activity: SignalActivity, failure_ns: Optional[float], window_span_ns: float) -> None:
    reasons: List[str] = []
    score = min(activity.changes, 50)

    if activity.xz_events:
        score += min(activity.xz_events * 3, 30)
        reasons.append("X/Z activity")

    lowered = activity.name.lower()
    for keyword, weight in KEYWORD_WEIGHTS.items():
        if keyword in lowered:
            score += weight
            reasons.append(f"name contains {keyword}")

    if failure_ns is not None and activity.last_change_ns is not None:
        distance = abs(activity.last_change_ns - failure_ns)
        proximity_bonus = max(0.0, 20.0 * (1.0 - min(distance, window_span_ns) / max(window_span_ns, 1.0)))
        if proximity_bonus:
            score += proximity_bonus
            reasons.append("changed near failure")

    activity.score = round(score, 2)
    activity.reasons = sorted(set(reasons))


def parse_vcd_activity(
    vcd_path: Path,
    *,
    start_ns: Optional[float],
    end_ns: Optional[float],
    failure_ns: Optional[float],
    max_events: int = 200_000,
) -> List[SignalActivity]:
    scope: List[str] = []
    symbols: Dict[str, VcdSymbol] = {}
    activities: Dict[str, SignalActivity] = {}
    timescale_ns = 1.0
    in_header = True
    in_timescale = False
    current_time_ns = 0.0
    events_seen = 0

    with vcd_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue

            if in_header:
                if line.startswith("$timescale"):
                    parsed = _timescale_to_ns(line)
                    if parsed is not None:
                        timescale_ns = parsed
                    else:
                        in_timescale = True
                    continue
                if in_timescale:
                    parsed = _timescale_to_ns(line)
                    if parsed is not None:
                        timescale_ns = parsed
                    if "$end" in line:
                        in_timescale = False
                    continue
                if line.startswith("$scope"):
                    parts = line.split()
                    if len(parts) >= 3:
                        scope.append(parts[2])
                    continue
                if line.startswith("$upscope"):
                    if scope:
                        scope.pop()
                    continue
                if line.startswith("$var"):
                    parts = line.split()
                    if len(parts) >= 5:
                        width = int(parts[2]) if parts[2].isdigit() else 1
                        id_code = parts[3]
                        ref_parts = []
                        for token in parts[4:]:
                            if token == "$end":
                                break
                            ref_parts.append(token)
                        reference = " ".join(ref_parts)
                        name = ".".join(scope + [reference])
                        symbols[id_code] = VcdSymbol(id_code=id_code, name=name, width=width)
                    continue
                if line.startswith("$enddefinitions"):
                    in_header = False
                    continue
                continue

            if line.startswith("#"):
                ticks = int(line[1:]) if line[1:].isdigit() else 0
                current_time_ns = ticks * timescale_ns
                if end_ns is not None and current_time_ns > end_ns:
                    break
                continue

            if start_ns is not None and current_time_ns < start_ns:
                continue

            id_code = ""
            value = ""
            first = line[0]
            if first in "01xXzZ":
                value = first.lower()
                id_code = line[1:].strip()
            elif first in "bBrR":
                parts = line[1:].split(None, 1)
                if len(parts) != 2:
                    continue
                value = parts[0].lower()
                id_code = parts[1].strip()
            else:
                continue

            symbol = symbols.get(id_code)
            if not symbol:
                continue
            _record_activity(activities, symbol, current_time_ns, value)
            events_seen += 1
            if events_seen >= max_events:
                break

    span = 1.0
    if start_ns is not None and end_ns is not None:
        span = max(end_ns - start_ns, 1.0)

    for activity in activities.values():
        _score_activity(activity, failure_ns, span)

    return sorted(activities.values(), key=lambda item: (-item.score, item.name))
