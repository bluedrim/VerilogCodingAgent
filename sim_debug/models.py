from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


@dataclass
class FailureEvent:
    kind: str
    severity: str
    line_number: int
    message: str
    context: List[str] = field(default_factory=list)
    time_value: Optional[float] = None
    time_unit: Optional[str] = None
    time_ns: Optional[float] = None
    source_file: Optional[str] = None
    source_line: Optional[int] = None
    hierarchy: Optional[str] = None
    signature: str = ""

    def time_label(self) -> str:
        if self.time_value is None or not self.time_unit:
            return "unknown"
        value = int(self.time_value) if float(self.time_value).is_integer() else self.time_value
        return f"{value} {self.time_unit}"


@dataclass
class EventCluster:
    kind: str
    severity: str
    signature: str
    count: int
    first_line: int
    first_time_ns: Optional[float]
    example: str


@dataclass
class LogAnalysis:
    log_path: Optional[Path]
    seed: Optional[str]
    events: List[FailureEvent]
    clusters: List[EventCluster]

    @property
    def first_failure(self) -> Optional[FailureEvent]:
        blocking = [
            event
            for event in self.events
            if event.severity in {"fatal", "error", "assertion", "scoreboard", "timeout"}
        ]
        if not blocking:
            return self.events[0] if self.events else None
        return blocking[0]


@dataclass
class WaveformWindow:
    center_ns: Optional[float]
    start_ns: Optional[float]
    end_ns: Optional[float]
    before_ns: float
    after_ns: float

    def label(self) -> str:
        if self.center_ns is None or self.start_ns is None or self.end_ns is None:
            return "unknown"
        return f"{self.start_ns:g} ns to {self.end_ns:g} ns"


@dataclass
class SignalActivity:
    name: str
    width: int
    changes: int = 0
    xz_events: int = 0
    first_change_ns: Optional[float] = None
    last_change_ns: Optional[float] = None
    first_xz_ns: Optional[float] = None
    first_value: Optional[str] = None
    last_value: Optional[str] = None
    values_seen: Set[str] = field(default_factory=set)
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)


@dataclass
class SourceHit:
    signal: str
    file: Path
    line_number: int
    role: str
    snippet: str
    score: int


@dataclass
class VectorSample:
    line_number: int
    sample_index: int
    raw: str
    value: str
    label: str = ""
    cycle: Optional[int] = None
    time_value: Optional[float] = None
    time_unit: Optional[str] = None
    time_ns: Optional[float] = None


@dataclass
class VectorMismatch:
    sample_index: int
    reference_line: int
    actual_line: int
    label: str
    cycle: Optional[int]
    time_ns: Optional[float]
    reference_value: str
    actual_value: str
    reference_raw: str
    actual_raw: str
    hamming_distance: Optional[int] = None
    differing_bits: List[int] = field(default_factory=list)


@dataclass
class VectorDiffResult:
    reference_path: Path
    actual_path: Path
    signal: Optional[str]
    compared_samples: int
    mismatches: List[VectorMismatch]
    extra_reference_samples: int = 0
    extra_actual_samples: int = 0

    @property
    def first_mismatch(self) -> Optional[VectorMismatch]:
        return self.mismatches[0] if self.mismatches else None


@dataclass
class ArtifactPaths:
    markdown: Path
    html: Path
    json: Path
    signal_list: Optional[Path] = None
    verdi_shell: Optional[Path] = None
    verdi_tcl: Optional[Path] = None


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(to_jsonable(item) for item in value)
    return value
