from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from .models import SourceHit


RTL_EXTENSIONS = {".v", ".sv", ".vh", ".svh"}
STOPWORDS = {
    "uvm_error",
    "uvm_fatal",
    "uvm_info",
    "expected",
    "actual",
    "scoreboard",
    "mismatch",
    "error",
    "fatal",
    "warning",
    "time",
    "line",
    "starting",
    "directed",
    "pipeline",
    "test",
    "run",
    "env",
    "uvm_test_top",
    "test_failed",
}
SIGNALISH_WORDS = {
    "clk",
    "clock",
    "reset",
    "rst",
    "rst_n",
    "state",
    "valid",
    "ready",
    "data",
    "addr",
    "enable",
    "done",
    "start",
    "last",
    "error",
    "fail",
}
SOURCE_PATH_RE = re.compile(r"[A-Za-z0-9_./+:-]+\.(?:sv|svh|v|vh)(?:[:(]\d+\)?)?", re.IGNORECASE)


def _strip_comment(line: str) -> str:
    return line.split("//", 1)[0]


def _resolve_filelist_entry(base: Path, raw: str) -> List[Path]:
    line = raw.strip()
    if not line or line.startswith("#") or line.startswith("//") or line.startswith("+incdir+"):
        return []
    if line.startswith("-f "):
        nested = (base / line.split(maxsplit=1)[1]).resolve()
        return collect_rtl_files(nested, [])
    if line.startswith("-v "):
        candidate = (base / line.split(maxsplit=1)[1]).resolve()
        return [candidate] if candidate.exists() and candidate.suffix.lower() in RTL_EXTENSIONS else []
    if line.startswith("-y"):
        return []

    candidate = Path(line)
    if not candidate.is_absolute():
        candidate = (base / candidate).resolve()
    return [candidate] if candidate.exists() and candidate.suffix.lower() in RTL_EXTENSIONS else []


def collect_rtl_files(filelist: Path | None, rtl_dirs: Sequence[Path]) -> List[Path]:
    files: List[Path] = []
    if filelist and filelist.exists():
        base = filelist.parent
        for raw in filelist.read_text(encoding="utf-8", errors="ignore").splitlines():
            files.extend(_resolve_filelist_entry(base, raw))

    for rtl_dir in rtl_dirs:
        if rtl_dir.is_file() and rtl_dir.suffix.lower() in RTL_EXTENSIONS:
            files.append(rtl_dir.resolve())
        elif rtl_dir.is_dir():
            for path in rtl_dir.rglob("*"):
                if path.suffix.lower() in RTL_EXTENSIONS:
                    files.append(path.resolve())

    return sorted(set(files))


def signal_leaf_name(signal: str) -> str:
    cleaned = re.sub(r"\[[^\]]+\]", "", signal.strip())
    cleaned = cleaned.strip().rstrip(",;")
    if "." in cleaned:
        cleaned = cleaned.split(".")[-1]
    return cleaned.strip("\\ ")


def extract_candidate_signals(text_blocks: Iterable[str]) -> List[str]:
    candidates: List[str] = []
    token_re = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_$]*\.)*[A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?")
    for block in text_blocks:
        cleaned_block = SOURCE_PATH_RE.sub(" ", block)
        cleaned_block = re.sub(r"0x[0-9a-fA-F_]+", " ", cleaned_block)
        for token in token_re.findall(cleaned_block):
            leaf = signal_leaf_name(token)
            if len(leaf) < 3 or leaf.lower() in STOPWORDS:
                continue
            if leaf.isupper() and "." not in token:
                continue
            signalish = "_" in leaf or "." in token or leaf.lower() in SIGNALISH_WORDS or bool(re.search(r"\d", leaf))
            if signalish and leaf not in candidates:
                candidates.append(leaf)
    return candidates


def _role_for_line(line: str) -> str:
    stripped = _strip_comment(line)
    if re.search(r"\b(input|output|inout)\b", stripped):
        return "port"
    if re.search(r"\b(wire|reg|logic)\b", stripped):
        return "declaration"
    if re.search(r"\bassign\b", stripped):
        return "continuous assign"
    if "<=" in stripped or re.search(r"(?<![=!<>])=(?!=)", stripped):
        return "assignment"
    if re.search(r"\balways(?:_ff|_comb|_latch)?\b", stripped):
        return "procedural block"
    return "reference"


def _role_score(role: str) -> int:
    return {
        "assignment": 50,
        "continuous assign": 45,
        "port": 35,
        "declaration": 30,
        "procedural block": 20,
        "reference": 10,
    }.get(role, 0)


def find_source_hits(
    signal_names: Sequence[str], rtl_files: Sequence[Path], max_hits_per_signal: int = 3
) -> Dict[str, List[SourceHit]]:
    results: Dict[str, List[SourceHit]] = {}
    unique_signals = []
    for signal in signal_names:
        leaf = signal_leaf_name(signal)
        if leaf and leaf not in unique_signals:
            unique_signals.append(leaf)

    for signal in unique_signals:
        pattern = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(signal)}(?![A-Za-z0-9_$])")
        hits: List[SourceHit] = []
        for path in rtl_files:
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if not pattern.search(_strip_comment(line)):
                    continue
                role = _role_for_line(line)
                score = _role_score(role)
                hits.append(
                    SourceHit(
                        signal=signal,
                        file=path,
                        line_number=line_number,
                        role=role,
                        snippet=line.strip(),
                        score=score,
                    )
                )
        results[signal] = sorted(hits, key=lambda hit: (-hit.score, str(hit.file), hit.line_number))[
            :max_hits_per_signal
        ]
    return results
