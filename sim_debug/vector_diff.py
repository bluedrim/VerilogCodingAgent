from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import VectorDiffResult, VectorMismatch, VectorSample
from .vcs_log import time_to_ns


TIME_TOKEN_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(fs|ps|ns|us|ms|s)$", re.IGNORECASE)
KEY_VALUE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_$]*)\s*=\s*([^,\s]+)")
VERILOG_LITERAL_RE = re.compile(r"^(?:(\d+)'([bBoOdDhH]))?([0-9a-fA-F_xXzZ]+)$")

METADATA_KEYS = {"cycle", "cyc", "index", "idx", "sample", "time", "t"}
DEFAULT_VALUE_KEYS = ("value", "val", "data", "out", "output", "out_data", "actual", "expected", "ref")
TIME_COLUMN_RE = re.compile(r"^(?:time|t)_?(fs|ps|ns|us|ms|s)$", re.IGNORECASE)


def _strip_comment(line: str) -> str:
    for marker in ("//", "#"):
        if marker in line:
            line = line.split(marker, 1)[0]
    return line.strip()


def _split_tokens(line: str) -> List[str]:
    return [token for token in re.split(r"[\s,]+", line.strip()) if token]


def _normalize_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_")


def _parse_time(token: str) -> Tuple[Optional[float], Optional[str], Optional[float]]:
    match = TIME_TOKEN_RE.match(token)
    if not match:
        return None, None, None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return value, unit, time_to_ns(value, unit)


def _parse_time_field(key: str, value: str) -> Tuple[Optional[float], Optional[str], Optional[float]]:
    value = value.strip()
    if not value:
        return None, None, None

    time_value, time_unit, time_ns = _parse_time(value)
    if time_ns is not None:
        return time_value, time_unit, time_ns

    match = TIME_COLUMN_RE.match(key)
    if match and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        unit = match.group(1).lower()
        numeric = float(value)
        return numeric, unit, time_to_ns(numeric, unit)

    if key in {"time", "t"} and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        numeric = float(value)
        return numeric, "ns", numeric

    return None, None, None


def _normalize_value(raw_value: str) -> str:
    value = raw_value.strip().rstrip(",;").replace("_", "")
    if not value:
        return value
    if value.lower().startswith("0x"):
        return value[2:].lower()

    match = VERILOG_LITERAL_RE.match(value)
    if match:
        width, base, digits = match.groups()
        digits = digits.lower()
        if base:
            base = base.lower()
            if base == "h":
                return digits
            if base == "b":
                return digits
            if base == "d" and digits.isdigit():
                bit_width = int(width) if width else max(1, int(digits).bit_length())
                return format(int(digits), f"0{bit_width}b")
            if base == "o" and re.fullmatch(r"[0-7]+", digits):
                bit_width = int(width) if width else len(digits) * 3
                return format(int(digits, 8), f"0{bit_width}b")
        return digits
    return value.lower()


def _value_to_bits(value: str) -> Optional[str]:
    normalized = _normalize_value(value)
    if not normalized:
        return None
    if any(ch in normalized for ch in "xz"):
        if re.fullmatch(r"[01xz]+", normalized):
            return normalized
        return None
    if re.fullmatch(r"[01]+", normalized) and len(normalized) > 1:
        return normalized
    if re.fullmatch(r"[0-9a-f]+", normalized):
        return "".join(format(int(ch, 16), "04b") for ch in normalized)
    return None


def _hamming(reference_value: str, actual_value: str) -> Tuple[Optional[int], List[int]]:
    reference_bits = _value_to_bits(reference_value)
    actual_bits = _value_to_bits(actual_value)
    if reference_bits is None or actual_bits is None:
        return None, []

    width = max(len(reference_bits), len(actual_bits))
    reference_bits = reference_bits.zfill(width)
    actual_bits = actual_bits.zfill(width)
    differing_bits: List[int] = []
    for bit_from_msb, (ref_bit, act_bit) in enumerate(zip(reference_bits, actual_bits)):
        if ref_bit != act_bit:
            differing_bits.append(width - 1 - bit_from_msb)
    return len(differing_bits), differing_bits


def _parse_key_values(line: str) -> Dict[str, str]:
    return {_normalize_key(match.group(1)): match.group(2) for match in KEY_VALUE_RE.finditer(line)}


def _choose_keyed_value(key_values: Dict[str, str], vector_signal: Optional[str]) -> Tuple[Optional[str], str]:
    if vector_signal:
        wanted = vector_signal.lower()
        for key, value in key_values.items():
            if key == wanted or key.endswith("." + wanted):
                return value, key
    for key in DEFAULT_VALUE_KEYS:
        if key in key_values:
            return key_values[key], key
    value_like = [(key, value) for key, value in key_values.items() if key not in METADATA_KEYS]
    if value_like:
        key, value = value_like[-1]
        return value, key
    return None, ""


def _parse_line(
    raw_line: str,
    line_number: int,
    sample_index: int,
    vector_signal: Optional[str],
    cycle_ns: Optional[float],
    start_time_ns: float,
) -> Optional[VectorSample]:
    cleaned = _strip_comment(raw_line)
    if not cleaned:
        return None

    key_values = _parse_key_values(cleaned)
    label = ""
    cycle: Optional[int] = None
    time_value: Optional[float] = None
    time_unit: Optional[str] = None
    time_ns: Optional[float] = None

    for key in ("cycle", "cyc", "index", "idx", "sample"):
        if key in key_values and re.fullmatch(r"\d+", key_values[key]):
            cycle = int(key_values[key])
            break
    for key in ("time", "t", "time_ns", "time_ps", "time_fs", "time_us", "time_ms", "time_s"):
        if key in key_values:
            time_value, time_unit, time_ns = _parse_time_field(key, key_values[key])
            break

    keyed_value, label = _choose_keyed_value(key_values, vector_signal)
    if keyed_value is not None:
        value = keyed_value
    else:
        tokens = _split_tokens(cleaned)
        value_tokens: List[str] = []
        for token in tokens:
            tv, tu, tns = _parse_time(token)
            if tns is not None:
                time_value, time_unit, time_ns = tv, tu, tns
                continue
            if "=" in token:
                continue
            if re.fullmatch(r"\d+", token) and cycle is None and len(tokens) > 1:
                cycle = int(token)
                continue
            value_tokens.append(token)
        if not value_tokens:
            return None
        value = value_tokens[-1]
        label = value_tokens[0] if len(value_tokens) > 1 else ""

    if time_ns is None and cycle_ns is not None:
        offset = cycle if cycle is not None else sample_index
        time_ns = start_time_ns + offset * cycle_ns
        time_value = time_ns
        time_unit = "ns"

    return VectorSample(
        line_number=line_number,
        sample_index=sample_index,
        raw=cleaned,
        value=_normalize_value(value),
        label=label,
        cycle=cycle,
        time_value=time_value,
        time_unit=time_unit,
        time_ns=time_ns,
    )


def _looks_like_delimited_header(cleaned_line: str) -> bool:
    if "=" in cleaned_line:
        return False
    delimiter = "\t" if "\t" in cleaned_line else "," if "," in cleaned_line else ""
    if not delimiter:
        return False
    cells = [_normalize_key(cell) for cell in next(csv.reader([cleaned_line], delimiter=delimiter))]
    if len(cells) < 2:
        return False
    known = set(DEFAULT_VALUE_KEYS) | METADATA_KEYS | {
        "time_ns",
        "time_ps",
        "time_fs",
        "time_us",
        "time_ms",
        "time_s",
    }
    return bool(known.intersection(cells)) or any(cell.endswith("_data") for cell in cells)


def _parse_delimited_sample(
    *,
    header: List[str],
    row: List[str],
    raw: str,
    line_number: int,
    sample_index: int,
    vector_signal: Optional[str],
    cycle_ns: Optional[float],
    start_time_ns: float,
) -> Optional[VectorSample]:
    if not row or all(not cell.strip() for cell in row):
        return None
    fields = {
        _normalize_key(header[index]): row[index].strip()
        for index in range(min(len(header), len(row)))
        if header[index].strip()
    }
    if not fields:
        return None

    cycle: Optional[int] = None
    for key in ("cycle", "cyc", "index", "idx", "sample"):
        value = fields.get(key, "")
        if re.fullmatch(r"\d+", value):
            cycle = int(value)
            break

    time_value: Optional[float] = None
    time_unit: Optional[str] = None
    time_ns: Optional[float] = None
    for key, value in fields.items():
        parsed_value, parsed_unit, parsed_ns = _parse_time_field(key, value)
        if parsed_ns is not None:
            time_value, time_unit, time_ns = parsed_value, parsed_unit, parsed_ns
            break

    value, label = _choose_keyed_value(fields, vector_signal)
    if value is None:
        return None

    if time_ns is None and cycle_ns is not None:
        offset = cycle if cycle is not None else sample_index
        time_ns = start_time_ns + offset * cycle_ns
        time_value = time_ns
        time_unit = "ns"

    return VectorSample(
        line_number=line_number,
        sample_index=sample_index,
        raw=raw,
        value=_normalize_value(value),
        label=label,
        cycle=cycle,
        time_value=time_value,
        time_unit=time_unit,
        time_ns=time_ns,
    )


def parse_vector_file(
    path: Path,
    *,
    vector_signal: Optional[str],
    cycle_ns: Optional[float],
    start_time_ns: float,
) -> List[VectorSample]:
    samples: List[VectorSample] = []
    raw_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    first_data_line = ""
    first_data_number = 0
    for line_number, raw_line in enumerate(raw_lines, start=1):
        cleaned = _strip_comment(raw_line)
        if cleaned:
            first_data_line = cleaned
            first_data_number = line_number
            break

    if first_data_line and _looks_like_delimited_header(first_data_line):
        delimiter = "\t" if "\t" in first_data_line else ","
        header = [_normalize_key(cell) for cell in next(csv.reader([first_data_line], delimiter=delimiter))]
        for line_number, raw_line in enumerate(raw_lines[first_data_number:], start=first_data_number + 1):
            cleaned = _strip_comment(raw_line)
            if not cleaned:
                continue
            row = next(csv.reader([cleaned], delimiter=delimiter))
            sample = _parse_delimited_sample(
                header=header,
                row=row,
                raw=cleaned,
                line_number=line_number,
                sample_index=len(samples),
                vector_signal=vector_signal,
                cycle_ns=cycle_ns,
                start_time_ns=start_time_ns,
            )
            if sample is not None:
                samples.append(sample)
        return samples

    for line_number, raw_line in enumerate(raw_lines, start=1):
        sample = _parse_line(
            raw_line,
            line_number,
            len(samples),
            vector_signal,
            cycle_ns,
            start_time_ns,
        )
        if sample is not None:
            samples.append(sample)
    return samples


def compare_vector_files(
    reference_path: Path,
    actual_path: Path,
    *,
    vector_signal: Optional[str] = None,
    cycle_ns: Optional[float] = None,
    start_time_ns: float = 0.0,
    max_mismatches: int = 20,
) -> VectorDiffResult:
    reference_samples = parse_vector_file(
        reference_path,
        vector_signal=vector_signal,
        cycle_ns=cycle_ns,
        start_time_ns=start_time_ns,
    )
    actual_samples = parse_vector_file(
        actual_path,
        vector_signal=vector_signal,
        cycle_ns=cycle_ns,
        start_time_ns=start_time_ns,
    )
    compared = min(len(reference_samples), len(actual_samples))
    mismatches: List[VectorMismatch] = []

    for index in range(compared):
        reference = reference_samples[index]
        actual = actual_samples[index]
        if reference.value == actual.value:
            continue
        hamming_distance, differing_bits = _hamming(reference.value, actual.value)
        label = actual.label or reference.label or f"sample_{index}"
        cycle = actual.cycle if actual.cycle is not None else reference.cycle
        time_ns = actual.time_ns if actual.time_ns is not None else reference.time_ns
        mismatches.append(
            VectorMismatch(
                sample_index=index,
                reference_line=reference.line_number,
                actual_line=actual.line_number,
                label=label,
                cycle=cycle,
                time_ns=time_ns,
                reference_value=reference.value,
                actual_value=actual.value,
                reference_raw=reference.raw,
                actual_raw=actual.raw,
                hamming_distance=hamming_distance,
                differing_bits=differing_bits[:32],
            )
        )
        if len(mismatches) >= max_mismatches:
            break

    return VectorDiffResult(
        reference_path=reference_path,
        actual_path=actual_path,
        signal=vector_signal,
        compared_samples=compared,
        mismatches=mismatches,
        extra_reference_samples=max(0, len(reference_samples) - compared),
        extra_actual_samples=max(0, len(actual_samples) - compared),
    )
