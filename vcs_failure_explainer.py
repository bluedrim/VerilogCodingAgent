#!/usr/bin/env python3
"""Explain VCS simulation failures using logs, FSDB launch metadata, and optional VCD activity."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from sim_debug.fsdb_adapter import discover_waveform_tools, write_verdi_launch_files
from sim_debug.models import ArtifactPaths, LogAnalysis, SignalActivity, VectorDiffResult, WaveformWindow, to_jsonable
from sim_debug.report import render_html, render_markdown
from sim_debug.rtl_mapper import collect_rtl_files, extract_candidate_signals, find_source_hits, signal_leaf_name
from sim_debug.vcd import parse_vcd_activity
from sim_debug.vcs_log import parse_vcs_log
from sim_debug.vector_diff import compare_vector_files


def non_negative_float(raw: str) -> float:
    value = float(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be 0 or greater")
    return value


def positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a VCS/FSDB simulation debug report. The MVP parses VCS logs, "
            "uses FSDB paths for Verdi launch artifacts, and ranks signals from an optional VCD export."
        )
    )
    parser.add_argument("--run-dir", type=Path, help="VCS run directory used to auto-detect log/fsdb/filelist files.")
    parser.add_argument("--log", type=Path, help="VCS simulation log, for example simv.log.")
    parser.add_argument("--fsdb", type=Path, help="FSDB waveform database.")
    parser.add_argument("--vcd", type=Path, help="Optional VCD converted from the FSDB failure window.")
    parser.add_argument("--reference-vector", type=Path, help="Golden/reference output vector file.")
    parser.add_argument("--actual-vector", type=Path, help="Saved DUT output vector file.")
    parser.add_argument(
        "--vector-signal",
        help="Signal/key/column name to compare when vector lines contain key=value fields.",
    )
    parser.add_argument(
        "--vector-cycle-ns",
        type=non_negative_float,
        help="Cycle period in ns used to map vector sample index/cycle to waveform time.",
    )
    parser.add_argument(
        "--vector-start-time-ns",
        type=non_negative_float,
        default=0.0,
        help="Time offset in ns for vector sample 0 when --vector-cycle-ns is used.",
    )
    parser.add_argument(
        "--max-vector-mismatches",
        type=positive_int,
        default=20,
        help="Maximum vector mismatches to include in the report.",
    )
    parser.add_argument("--filelist", type=Path, help="RTL compile filelist used for source mapping and Verdi launch.")
    parser.add_argument(
        "--rtl-dir",
        type=Path,
        action="append",
        default=[],
        help="RTL source directory or file. Can be passed multiple times.",
    )
    parser.add_argument("--top", help="Optional top module for generated Verdi launch command.")
    parser.add_argument("--out-dir", type=Path, default=Path("sim_debug_report"), help="Output report directory.")
    parser.add_argument("--before-ns", type=non_negative_float, default=100.0, help="Waveform window before failure.")
    parser.add_argument("--after-ns", type=non_negative_float, default=20.0, help="Waveform window after failure.")
    parser.add_argument("--max-signals", type=positive_int, default=20, help="Maximum ranked signals in the report.")
    parser.add_argument(
        "--max-source-hits",
        type=positive_int,
        default=3,
        help="Maximum RTL source hits per signal.",
    )
    parser.add_argument(
        "--max-vcd-events",
        type=positive_int,
        default=200_000,
        help="Maximum VCD value changes to scan inside the window.",
    )
    parser.add_argument(
        "--context-lines",
        type=positive_int,
        default=1,
        help="Log context lines around each detected event.",
    )
    return parser


def _first_existing(run_dir: Optional[Path], names: List[str]) -> Optional[Path]:
    if not run_dir:
        return None
    for name in names:
        candidate = run_dir / name
        if candidate.exists():
            return candidate
    return None


def _first_glob(run_dir: Optional[Path], pattern: str) -> Optional[Path]:
    if not run_dir:
        return None
    matches = sorted(run_dir.glob(pattern))
    return matches[0] if matches else None


def _resolve_input_files(args: argparse.Namespace) -> argparse.Namespace:
    run_dir = args.run_dir.resolve() if args.run_dir else None
    if run_dir and not run_dir.exists():
        raise FileNotFoundError(f"--run-dir does not exist: {run_dir}")

    args.log = args.log or _first_existing(run_dir, ["simv.log", "vcs.log", "run.log"]) or _first_glob(run_dir, "*.log")
    args.fsdb = args.fsdb or _first_glob(run_dir, "*.fsdb")
    args.filelist = args.filelist or _first_existing(run_dir, ["filelist.f", "compile_order.f"]) or _first_glob(run_dir, "*.f")
    args.vcd = args.vcd or _first_glob(run_dir, "*.vcd")
    args.reference_vector = args.reference_vector or _first_existing(
        run_dir, ["reference.vec", "ref.vec", "expected.vec", "golden.vec"]
    )
    args.actual_vector = args.actual_vector or _first_existing(
        run_dir, ["actual.vec", "output.vec", "dut_output.vec", "saved.vec"]
    )

    if bool(args.reference_vector) != bool(args.actual_vector):
        raise FileNotFoundError("Pass both --reference-vector and --actual-vector, or neither.")
    if not args.log and not (args.reference_vector and args.actual_vector):
        raise FileNotFoundError(
            "No VCS log or vector pair was provided. Pass --log, --run-dir, or both vector files."
        )
    for attr in ["log", "fsdb", "vcd", "filelist", "reference_vector", "actual_vector"]:
        path = getattr(args, attr)
        if path is not None:
            resolved = path.resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"--{attr} does not exist: {resolved}")
            setattr(args, attr, resolved)
    args.rtl_dir = [path.resolve() for path in args.rtl_dir]
    args.out_dir = args.out_dir.resolve()
    return args


def _ranked_signal_names(
    activities: List[SignalActivity], fallback_text: List[str], max_signals: int, seed_signals: List[str]
) -> List[str]:
    names: List[str] = []
    seed_leafs = {signal_leaf_name(signal).lower() for signal in seed_signals if signal}

    for signal in seed_signals:
        if signal and signal not in names:
            names.append(signal)
        if len(names) >= max_signals:
            return names[:max_signals]

    for activity in activities:
        if signal_leaf_name(activity.name).lower() in seed_leafs and activity.name not in names:
            names.append(activity.name)
        if len(names) >= max_signals:
            return names[:max_signals]

    for activity in activities:
        if activity.name not in names:
            names.append(activity.name)
        if len(names) >= max_signals:
            return names[:max_signals]

    if activities:
        return names[:max_signals]

    for candidate in extract_candidate_signals(fallback_text):
        if candidate not in names:
            names.append(candidate)
        if len(names) >= max_signals:
            break
    return names[:max_signals]


def _build_window(
    first_failure_time_ns: Optional[float],
    before_ns: float,
    after_ns: float,
) -> WaveformWindow:
    if first_failure_time_ns is None:
        return WaveformWindow(None, None, None, before_ns, after_ns)
    return WaveformWindow(
        first_failure_time_ns,
        max(0.0, first_failure_time_ns - before_ns),
        first_failure_time_ns + after_ns,
        before_ns,
        after_ns,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = _resolve_input_files(parser.parse_args(argv))
    args.out_dir.mkdir(parents=True, exist_ok=True)

    log_analysis = (
        parse_vcs_log(args.log, context_lines=args.context_lines)
        if args.log
        else LogAnalysis(log_path=None, seed=None, events=[], clusters=[])
    )
    first_failure = log_analysis.first_failure

    vector_diff: Optional[VectorDiffResult] = None
    if args.reference_vector and args.actual_vector:
        vector_diff = compare_vector_files(
            args.reference_vector,
            args.actual_vector,
            vector_signal=args.vector_signal,
            cycle_ns=args.vector_cycle_ns,
            start_time_ns=args.vector_start_time_ns,
            max_mismatches=args.max_vector_mismatches,
        )

    vector_mismatch = vector_diff.first_mismatch if vector_diff else None
    window_center_ns = vector_mismatch.time_ns if vector_mismatch and vector_mismatch.time_ns is not None else None
    if window_center_ns is None:
        window_center_ns = first_failure.time_ns if first_failure else None
    window = _build_window(window_center_ns, args.before_ns, args.after_ns)
    tools = discover_waveform_tools()

    activities: List[SignalActivity] = []
    if args.vcd:
        activities = parse_vcd_activity(
            args.vcd,
            start_ns=window.start_ns,
            end_ns=window.end_ns,
            failure_ns=window.center_ns,
            max_events=args.max_vcd_events,
        )[: args.max_signals]

    fallback_text: List[str] = []
    if first_failure:
        fallback_text.extend(first_failure.context or [first_failure.message])
    fallback_text.extend(event.message for event in log_analysis.events[:10])
    if vector_mismatch:
        fallback_text.extend([vector_mismatch.reference_raw, vector_mismatch.actual_raw, vector_mismatch.label])

    seed_signals = []
    if args.vector_signal:
        seed_signals.append(args.vector_signal)
    if vector_mismatch and vector_mismatch.label and not vector_mismatch.label.startswith("sample_"):
        seed_signals.append(vector_mismatch.label)

    ranked_signals = _ranked_signal_names(activities, fallback_text, args.max_signals, seed_signals)
    rtl_files = collect_rtl_files(args.filelist, args.rtl_dir)
    source_hits = find_source_hits(ranked_signals, rtl_files, max_hits_per_signal=args.max_source_hits) if rtl_files else {}

    generated = write_verdi_launch_files(
        out_dir=args.out_dir,
        fsdb_path=args.fsdb,
        filelist=args.filelist,
        top=args.top,
        signals=ranked_signals,
        window=window,
    )

    artifact_paths = ArtifactPaths(
        markdown=args.out_dir / "debug_report.md",
        html=args.out_dir / "debug_report.html",
        json=args.out_dir / "debug_summary.json",
        signal_list=generated.get("signal_list"),
        verdi_shell=generated.get("verdi_shell"),
        verdi_tcl=generated.get("verdi_tcl"),
    )

    markdown = render_markdown(
        log_analysis=log_analysis,
        window=window,
        fsdb_path=args.fsdb,
        vcd_path=args.vcd,
        tools=tools,
        activities=activities,
        vector_diff=vector_diff,
        ranked_signals=ranked_signals,
        source_hits=source_hits,
        artifacts=artifact_paths,
    )
    artifact_paths.markdown.write_text(markdown, encoding="utf-8")
    artifact_paths.html.write_text(render_html(markdown), encoding="utf-8")

    summary = {
        "log_analysis": log_analysis,
        "waveform_window": window,
        "fsdb_path": args.fsdb,
        "vcd_path": args.vcd,
        "vector_diff": vector_diff,
        "filelist": args.filelist,
        "rtl_file_count": len(rtl_files),
        "ranked_signals": ranked_signals,
        "activities": activities,
        "source_hits": source_hits,
        "tools": tools,
        "artifacts": artifact_paths,
    }
    artifact_paths.json.write_text(json.dumps(to_jsonable(summary), indent=2), encoding="utf-8")

    print(f"Wrote debug report: {artifact_paths.markdown}")
    print(f"Wrote HTML report:  {artifact_paths.html}")
    if artifact_paths.verdi_shell:
        print(f"Wrote Verdi launch: {artifact_paths.verdi_shell}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
