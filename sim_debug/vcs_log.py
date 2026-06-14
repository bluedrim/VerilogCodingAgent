from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .models import EventCluster, FailureEvent, LogAnalysis


TIME_FACTORS_TO_NS = {
    "fs": 1e-6,
    "ps": 1e-3,
    "ns": 1.0,
    "us": 1e3,
    "ms": 1e6,
    "s": 1e9,
}

TIME_RE = re.compile(
    r"(?:@|at|time\s*=?\s*)\s*([0-9]+(?:\.[0-9]+)?)\s*(fs|ps|ns|us|ms|s)\b",
    re.IGNORECASE,
)
LOOSE_TIME_RE = re.compile(r"\b([0-9]+(?:\.[0-9]+)?)\s*(fs|ps|ns|us|ms|s)\b", re.IGNORECASE)
SEED_RE = re.compile(r"(?:\+?ntb_random_seed|seed)\s*[=:]\s*([0-9A-Za-z_+-]+)", re.IGNORECASE)
SOURCE_RE = re.compile(
    r"(?:(?:\"([^\"]+\.(?:sv|svh|v|vh))\",\s*line\s*(\d+))|"
    r"([A-Za-z0-9_./+:-]+\.(?:sv|svh|v|vh))[:(](\d+))",
    re.IGNORECASE,
)
HIER_RE = re.compile(r"\b(?:uvm_test_top|testbench|tb|top|dut)\.[A-Za-z0-9_$.\[\]:]+")

EVENT_PATTERNS: List[Tuple[str, str, re.Pattern[str]]] = [
    ("UVM_FATAL", "fatal", re.compile(r"\bUVM_FATAL\b")),
    ("UVM_ERROR", "error", re.compile(r"\bUVM_ERROR\b")),
    (
        "ASSERTION",
        "assertion",
        re.compile(r"\b(?:assertion|assert|sva|property)\b.{0,120}\b(?:fail|failed|error)\b", re.IGNORECASE),
    ),
    (
        "SCOREBOARD_MISMATCH",
        "scoreboard",
        re.compile(r"\b(?:scoreboard|mismatch|expected|actual)\b", re.IGNORECASE),
    ),
    ("TIMEOUT", "timeout", re.compile(r"\b(?:timeout|watchdog|hang detected)\b", re.IGNORECASE)),
    (
        "VCS_FATAL",
        "fatal",
        re.compile(r"\b(?:Fatal:|\$fatal|SIMULATION FAILED|TEST FAILED)\b", re.IGNORECASE),
    ),
    (
        "VCS_ERROR",
        "error",
        re.compile(r"\b(?:Error-\[|Error:|FAIL\b|FAILED\b)\b", re.IGNORECASE),
    ),
]


def time_to_ns(value: float, unit: str) -> float:
    return value * TIME_FACTORS_TO_NS[unit.lower()]


def _extract_time(line: str) -> Tuple[Optional[float], Optional[str], Optional[float]]:
    match = TIME_RE.search(line) or LOOSE_TIME_RE.search(line)
    if not match:
        return None, None, None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return value, unit, time_to_ns(value, unit)


def _extract_source(line: str) -> Tuple[Optional[str], Optional[int]]:
    match = SOURCE_RE.search(line)
    if not match:
        return None, None
    file_name = match.group(1) or match.group(3)
    line_number = match.group(2) or match.group(4)
    return file_name, int(line_number)


def _extract_hierarchy(line: str) -> Optional[str]:
    cleaned = SOURCE_RE.sub(" ", line)
    match = HIER_RE.search(cleaned)
    return match.group(0) if match else None


def _normalize_signature(line: str) -> str:
    normalized = re.sub(r"\b[0-9]+(?:\.[0-9]+)?\s*(?:fs|ps|ns|us|ms|s)\b", "<time>", line, flags=re.I)
    normalized = re.sub(r"0x[0-9a-fA-F_]+", "<hex>", normalized)
    normalized = re.sub(r"\b\d+\b", "<num>", normalized)
    normalized = re.sub(r"[A-Za-z0-9_./+:-]+\.(?:sv|svh|v|vh)[:(]?\d*?", "<source>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized[:180]


def _classify_event(line: str) -> Optional[Tuple[str, str]]:
    for kind, severity, pattern in EVENT_PATTERNS:
        if pattern.search(line):
            return kind, severity
    return None


def _find_seed(lines: Iterable[str]) -> Optional[str]:
    for line in lines:
        match = SEED_RE.search(line)
        if match:
            return match.group(1)
    return None


def _context(lines: List[str], index: int, context_lines: int) -> List[str]:
    start = max(0, index - context_lines)
    end = min(len(lines), index + context_lines + 1)
    return [lines[i].rstrip() for i in range(start, end)]


def _build_clusters(events: List[FailureEvent]) -> List[EventCluster]:
    grouped: "OrderedDict[Tuple[str, str, str], EventCluster]" = OrderedDict()
    for event in events:
        key = (event.kind, event.severity, event.signature)
        if key not in grouped:
            grouped[key] = EventCluster(
                kind=event.kind,
                severity=event.severity,
                signature=event.signature,
                count=0,
                first_line=event.line_number,
                first_time_ns=event.time_ns,
                example=event.message,
            )
        grouped[key].count += 1
    return sorted(grouped.values(), key=lambda cluster: (-cluster.count, cluster.first_line))


def parse_vcs_log(log_path: Path, context_lines: int = 1) -> LogAnalysis:
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    seed = _find_seed(lines)
    events: List[FailureEvent] = []
    last_time: Tuple[Optional[float], Optional[str], Optional[float]] = (None, None, None)

    for index, line in enumerate(lines):
        value, unit, ns = _extract_time(line)
        if ns is not None:
            last_time = (value, unit, ns)

        classification = _classify_event(line)
        if not classification:
            continue

        kind, severity = classification
        source_file, source_line = _extract_source(line)
        hierarchy = _extract_hierarchy(line)

        if ns is None:
            value, unit, ns = last_time

        message = line.strip()
        events.append(
            FailureEvent(
                kind=kind,
                severity=severity,
                line_number=index + 1,
                message=message,
                context=_context(lines, index, context_lines),
                time_value=value,
                time_unit=unit,
                time_ns=ns,
                source_file=source_file,
                source_line=source_line,
                hierarchy=hierarchy,
                signature=_normalize_signature(message),
            )
        )

    return LogAnalysis(log_path=log_path, seed=seed, events=events, clusters=_build_clusters(events))
