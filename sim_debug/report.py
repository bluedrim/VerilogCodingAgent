from __future__ import annotations

import html
from pathlib import Path
from typing import Dict, List, Optional

from .models import ArtifactPaths, LogAnalysis, SignalActivity, SourceHit, VectorDiffResult, WaveformWindow


def _format_ns(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    return f"{value:g} ns"


def _rel(path: Path) -> str:
    return str(path)


def _markdown_table(headers: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return ""
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(cell.replace("\n", " ") for cell in row) + " |")
    return "\n".join(out)


def render_markdown(
    *,
    log_analysis: LogAnalysis,
    window: WaveformWindow,
    fsdb_path: Optional[Path],
    vcd_path: Optional[Path],
    tools: Dict[str, str],
    activities: List[SignalActivity],
    vector_diff: Optional[VectorDiffResult],
    ranked_signals: List[str],
    source_hits: Dict[str, List[SourceHit]],
    artifacts: ArtifactPaths,
) -> str:
    first = log_analysis.first_failure
    first_vector_mismatch = vector_diff.first_mismatch if vector_diff else None
    lines: List[str] = ["# VCS Simulation Debug Report", ""]

    lines.extend(
        [
            "## Failure Summary",
            "",
            f"- Log: `{log_analysis.log_path if log_analysis.log_path else 'not provided'}`",
            f"- Seed: `{log_analysis.seed or 'unknown'}`",
            f"- First failure kind: `{first.kind if first else 'not found'}`",
            f"- First failure time: `{first.time_label() if first else 'unknown'}`",
            f"- First vector mismatch: `{first_vector_mismatch.label if first_vector_mismatch else 'not provided/found'}`",
            f"- Failure window: `{window.label()}`",
            f"- FSDB: `{fsdb_path if fsdb_path else 'not provided'}`",
            f"- VCD analyzed: `{vcd_path if vcd_path else 'not provided'}`",
            "",
        ]
    )

    if first:
        lines.extend(["### First Failure Context", "", "```text"])
        lines.extend(first.context or [first.message])
        lines.extend(["```", ""])

    if first_vector_mismatch or ranked_signals:
        lines.extend(["## Debug Focus", ""])
        if first_vector_mismatch:
            lines.extend(
                [
                    f"- First output mismatch label: `{first_vector_mismatch.label}`",
                    f"- First output mismatch sample: `{first_vector_mismatch.sample_index}`",
                    f"- First output mismatch cycle: `{first_vector_mismatch.cycle if first_vector_mismatch.cycle is not None else 'unknown'}`",
                    f"- First output mismatch time: `{_format_ns(first_vector_mismatch.time_ns)}`",
                    f"- Reference vs actual: `{first_vector_mismatch.reference_value}` vs `{first_vector_mismatch.actual_value}`",
                ]
            )
            if first_vector_mismatch.differing_bits:
                bits = ", ".join(str(bit) for bit in first_vector_mismatch.differing_bits[:16])
                if len(first_vector_mismatch.differing_bits) > 16:
                    bits += ", ..."
                lines.append(f"- Differing output bits: `{bits}`")
        if ranked_signals:
            lines.append("- Open these signals first in Verdi:")
            for signal in ranked_signals[:8]:
                lines.append(f"  - `{signal}`")
        lines.append("")

    if log_analysis.clusters:
        rows = [
            [
                cluster.severity,
                cluster.kind,
                str(cluster.count),
                str(cluster.first_line),
                _format_ns(cluster.first_time_ns),
                cluster.example[:120],
            ]
            for cluster in log_analysis.clusters[:12]
        ]
        lines.extend(["## Error Clusters", "", _markdown_table(
            ["Severity", "Kind", "Count", "First line", "First time", "Example"], rows
        ), ""])

    lines.extend(["## Waveform Analysis", ""])
    if activities:
        rows = []
        for activity in activities[:20]:
            rows.append(
                [
                    f"{activity.score:g}",
                    activity.name,
                    str(activity.changes),
                    str(activity.xz_events),
                    _format_ns(activity.last_change_ns),
                    ", ".join(activity.reasons[:4]) or "-",
                ]
            )
        lines.extend([_markdown_table(["Score", "Signal", "Changes", "X/Z", "Last change", "Why"], rows), ""])
    else:
        lines.extend(
            [
                "No VCD activity was analyzed. Provide `--vcd <converted.vcd>` to rank signals from the failure window.",
                "",
            ]
        )

    if vector_diff:
        lines.extend(["## Vector Diff", ""])
        lines.extend(
            [
                f"- Reference vector: `{vector_diff.reference_path}`",
                f"- Actual vector: `{vector_diff.actual_path}`",
                f"- Compared samples: `{vector_diff.compared_samples}`",
                f"- Mismatches shown: `{len(vector_diff.mismatches)}`",
                f"- Extra reference samples: `{vector_diff.extra_reference_samples}`",
                f"- Extra actual samples: `{vector_diff.extra_actual_samples}`",
                "",
            ]
        )
        if vector_diff.mismatches:
            rows = []
            for mismatch in vector_diff.mismatches[:20]:
                bit_text = ", ".join(str(bit) for bit in mismatch.differing_bits[:12])
                if len(mismatch.differing_bits) > 12:
                    bit_text += ", ..."
                rows.append(
                    [
                        str(mismatch.sample_index),
                        mismatch.label,
                        str(mismatch.cycle) if mismatch.cycle is not None else "-",
                        _format_ns(mismatch.time_ns),
                        mismatch.reference_value,
                        mismatch.actual_value,
                        str(mismatch.hamming_distance) if mismatch.hamming_distance is not None else "-",
                        bit_text or "-",
                    ]
                )
            lines.extend(
                [
                    _markdown_table(
                        ["Sample", "Label", "Cycle", "Time", "Reference", "Actual", "Bit diff", "Diff bits"],
                        rows,
                    ),
                    "",
                ]
            )
            first_mismatch = vector_diff.first_mismatch
            if first_mismatch:
                lines.extend(
                    [
                        "### First Vector Mismatch Raw Lines",
                        "",
                        "```text",
                        f"reference:{first_mismatch.reference_line}: {first_mismatch.reference_raw}",
                        f"actual:   {first_mismatch.actual_line}: {first_mismatch.actual_raw}",
                        "```",
                        "",
                    ]
                )
        else:
            lines.extend(["No vector mismatches were found in the compared samples.", ""])

    if source_hits:
        lines.extend(["## RTL Source Candidates", ""])
        for signal, hits in source_hits.items():
            if not hits:
                continue
            lines.append(f"### `{signal}`")
            rows = [
                [hit.role, f"`{hit.file}:{hit.line_number}`", f"`{hit.snippet[:160]}`"]
                for hit in hits
            ]
            lines.extend([_markdown_table(["Role", "Location", "Snippet"], rows), ""])

    lines.extend(["## Generated Artifacts", ""])
    artifact_rows = [
        ["Markdown report", f"`{artifacts.markdown}`"],
        ["HTML report", f"`{artifacts.html}`"],
        ["JSON summary", f"`{artifacts.json}`"],
    ]
    if artifacts.signal_list:
        artifact_rows.append(["Ranked signal list", f"`{artifacts.signal_list}`"])
    if artifacts.verdi_shell:
        artifact_rows.append(["Verdi shell launcher", f"`{artifacts.verdi_shell}`"])
    if artifacts.verdi_tcl:
        artifact_rows.append(["Verdi Tcl notes", f"`{artifacts.verdi_tcl}`"])
    lines.extend([_markdown_table(["Artifact", "Path"], artifact_rows), ""])

    lines.extend(["## Environment Notes", ""])
    if tools:
        lines.append("Detected waveform tools:")
        for name, path in tools.items():
            lines.append(f"- `{name}`: `{path}`")
    else:
        lines.append("No Verdi/FSDB command-line tools were found on `PATH` during this run.")
    lines.extend(
        [
            "",
            "FSDB is treated as the canonical waveform database. Native FSDB parsing depends on Verdi/Novas utilities, so this MVP analyzes VCD when supplied and generates Verdi launch artifacts for the original FSDB.",
            "",
        ]
    )

    return "\n".join(lines)


def render_html(markdown: str, title: str = "VCS Simulation Debug Report") -> str:
    body_lines = []
    in_code = False
    table_buffer: List[str] = []

    def flush_table() -> None:
        nonlocal table_buffer
        if len(table_buffer) < 2:
            table_buffer = []
            return
        headers = [cell.strip() for cell in table_buffer[0].strip("|").split("|")]
        rows = table_buffer[2:]
        body_lines.append("<table>")
        body_lines.append("<thead><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in headers) + "</tr></thead>")
        body_lines.append("<tbody>")
        for row in rows:
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            body_lines.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
        body_lines.append("</tbody></table>")
        table_buffer = []

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            table_buffer.append(line)
            continue
        flush_table()
        if line.startswith("```"):
            if in_code:
                body_lines.append("</code></pre>")
                in_code = False
            else:
                body_lines.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            body_lines.append(html.escape(line))
            continue
        if line.startswith("# "):
            body_lines.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            body_lines.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("### "):
            body_lines.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("- "):
            body_lines.append(f"<p>{html.escape(line)}</p>")
        elif not line:
            body_lines.append("")
        else:
            body_lines.append(f"<p>{html.escape(line)}</p>")
    flush_table()

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2933; }}
    h1, h2, h3 {{ color: #102a43; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px; font-size: 14px; }}
    th, td {{ border: 1px solid #d9e2ec; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #f0f4f8; }}
    pre {{ background: #102a43; color: #f0f4f8; padding: 16px; overflow-x: auto; }}
    code {{ font-family: "SFMono-Regular", Consolas, monospace; }}
    p {{ line-height: 1.5; }}
  </style>
</head>
<body>
{chr(10).join(body_lines)}
</body>
</html>
"""
