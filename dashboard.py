#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - dashboard still works without .env loading
    load_dotenv = None


ROOT = Path(__file__).resolve().parent
MAX_FILE_PREVIEW_BYTES = 1_000_000
MAX_START_PAYLOAD_BYTES = 1_000_000
MAX_SPEC_CHARS = 500_000


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Verilog Agent Dashboard</title>
  <style>
    :root {
      --bg: #f5f7f9;
      --panel: #ffffff;
      --ink: #17212b;
      --muted: #667789;
      --line: #d8e0e8;
      --good: #16805b;
      --warn: #9b6500;
      --bad: #b3261e;
      --info: #2454a6;
      --soft-good: #e7f4ee;
      --soft-warn: #fff3d8;
      --soft-bad: #fdebea;
      --soft-info: #eaf0fb;
      --shadow: 0 1px 2px rgba(16, 24, 40, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 20px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 3;
    }
    h1 {
      font-size: 18px;
      line-height: 1.2;
      margin: 0;
      font-weight: 700;
    }
    main {
      padding: 18px 20px 28px;
      max-width: 1440px;
      margin: 0 auto;
    }
    select, button {
      min-height: 34px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      padding: 6px 10px;
      font: inherit;
    }
    button { cursor: pointer; }
    button:hover { border-color: #9fb0c3; }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .launch-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(320px, 0.6fr);
      gap: 12px;
      margin-bottom: 14px;
    }
    .launch-grid textarea,
    .launch-grid input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }
    .launch-grid textarea {
      min-height: 150px;
      resize: vertical;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 5px;
      margin-bottom: 9px;
    }
    .field label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .option-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 8px;
    }
    .launch-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .status-line {
      color: var(--muted);
      font-size: 12px;
      min-height: 18px;
      overflow-wrap: anywhere;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 14px;
    }
    .panel h2 {
      margin: 0 0 10px;
      font-size: 14px;
      line-height: 1.2;
      color: #223142;
    }
    .metric {
      display: flex;
      flex-direction: column;
      gap: 5px;
      min-height: 86px;
    }
    .metric .label {
      color: var(--muted);
      font-size: 12px;
    }
    .metric .value {
      font-size: 22px;
      font-weight: 750;
      overflow-wrap: anywhere;
    }
    .metric .sub {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid var(--line);
      color: var(--muted);
      background: #f7f9fb;
    }
    .pill.good { color: var(--good); background: var(--soft-good); border-color: #b7dfcf; }
    .pill.warn { color: var(--warn); background: var(--soft-warn); border-color: #f2d28c; }
    .pill.bad { color: var(--bad); background: var(--soft-bad); border-color: #f4b9b4; }
    .pill.info { color: var(--info); background: var(--soft-info); border-color: #bfd0ef; }
    .two-col {
      display: grid;
      grid-template-columns: minmax(0, 1.15fr) minmax(360px, 0.85fr);
      gap: 12px;
    }
    .pipeline {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .stage {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      min-height: 94px;
      background: #fbfcfd;
    }
    .stage.pass { background: var(--soft-good); border-color: #b7dfcf; }
    .stage.force { background: var(--soft-warn); border-color: #f2d28c; }
    .stage.fail { background: var(--soft-bad); border-color: #f4b9b4; }
    .stage.active { background: var(--soft-info); border-color: #bfd0ef; }
    .stage-title {
      font-weight: 750;
      font-size: 13px;
      margin-bottom: 6px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .stage-meta {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .bar {
      height: 7px;
      border-radius: 99px;
      background: #e5ebf1;
      overflow: hidden;
      margin-top: 8px;
    }
    .bar span {
      display: block;
      height: 100%;
      width: 0;
      background: var(--info);
    }
    .stage.force .bar span { background: var(--warn); }
    .stage.fail .bar span { background: var(--bad); }
    .stage.pass .bar span { background: var(--good); }
    .lists {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 12px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      text-align: left;
      padding: 7px 6px;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 700;
      background: #fafbfd;
    }
    td.path {
      max-width: 440px;
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    a.file-link {
      color: var(--info);
      text-decoration: none;
    }
    a.file-link:hover { text-decoration: underline; }
    pre {
      margin: 0;
      max-height: 360px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
      color: #1f2a36;
      background: #f8fafc;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
    }
    .report-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .report-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      min-height: 96px;
      background: #fbfcfd;
    }
    .report-item h3 {
      margin: 0 0 7px;
      font-size: 12px;
      color: var(--muted);
    }
    .report-item p {
      margin: 0;
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
      max-height: 120px;
      overflow: auto;
    }
    .empty {
      color: var(--muted);
      font-size: 12px;
      padding: 8px 0;
    }
    .footer-note {
      color: var(--muted);
      font-size: 12px;
      margin-top: 12px;
    }
    @media (max-width: 980px) {
      header { align-items: flex-start; flex-direction: column; }
      .grid, .two-col, .lists, .report-grid, .launch-grid { grid-template-columns: 1fr; }
      .pipeline { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      main { padding: 12px; }
      .pipeline { grid-template-columns: 1fr; }
      .toolbar { width: 100%; }
      select { width: 100%; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Verilog Agent Dashboard</h1>
      <div id="subtitle" class="footer-note">Loading runs...</div>
    </div>
    <div class="toolbar">
      <select id="runSelect" aria-label="Run selector"></select>
      <button id="refreshBtn" type="button">Refresh</button>
      <span id="autoState" class="pill info">Auto 2s</span>
    </div>
  </header>
  <main>
    <section class="panel" style="margin-bottom:14px;">
      <h2>Start Agent Run</h2>
      <form id="startForm">
        <div class="launch-grid">
          <div>
            <div class="field">
              <label for="specText">Requirement or Markdown content</label>
              <textarea id="specText" placeholder="Describe the RTL requirement here, or choose a .md file below."></textarea>
            </div>
            <div class="field">
              <label for="specFile">Markdown input file</label>
              <input id="specFile" type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" />
            </div>
          </div>
          <div>
            <div class="option-row">
              <div class="field">
                <label for="llmProvider">LLM provider</label>
                <select id="llmProvider">
                  <option value="">Default</option>
                  <option value="ollama">ollama</option>
                  <option value="gpt-oss">gpt-oss</option>
                  <option value="openai">openai</option>
                  <option value="codex">codex</option>
                </select>
              </div>
            </div>
            <div class="status-line">
              Model, API, retry, lint, and testbench options are read from .env or environment variables.
            </div>
            <div class="launch-actions">
              <button id="startBtn" type="submit">Start Run</button>
              <span id="startStatus" class="status-line">Ready.</span>
            </div>
          </div>
        </div>
      </form>
    </section>
    <section class="grid" id="metrics"></section>
    <section class="two-col">
      <div class="panel">
        <h2>Pipeline</h2>
        <div class="pipeline" id="pipeline"></div>
      </div>
      <div class="panel">
        <h2>Latest Report</h2>
        <pre id="latestReport">No report yet.</pre>
      </div>
    </section>
    <section class="panel" style="margin-top:12px;">
      <h2>Stage Reports</h2>
      <div class="report-grid" id="reports"></div>
    </section>
    <section class="lists">
      <div class="panel">
        <h2>Recent Artifacts</h2>
        <div id="artifactList"></div>
      </div>
      <div class="panel">
        <h2>Failed Attempts</h2>
        <div id="failedList"></div>
      </div>
    </section>
    <section class="panel" style="margin-top:12px;">
      <h2>File Preview</h2>
      <pre id="filePreview">Select an artifact to preview it.</pre>
    </section>
  </main>
  <script>
    const state = { runs: [], selected: "", timer: null, lastRun: null };
    const runSelect = document.getElementById("runSelect");
    const subtitle = document.getElementById("subtitle");
    const metrics = document.getElementById("metrics");
    const pipeline = document.getElementById("pipeline");
    const reports = document.getElementById("reports");
    const latestReport = document.getElementById("latestReport");
    const artifactList = document.getElementById("artifactList");
    const failedList = document.getElementById("failedList");
    const filePreview = document.getElementById("filePreview");
    const startForm = document.getElementById("startForm");
    const specText = document.getElementById("specText");
    const specFile = document.getElementById("specFile");
    const startBtn = document.getElementById("startBtn");
    const startStatus = document.getElementById("startStatus");

    document.getElementById("refreshBtn").addEventListener("click", () => refreshAll(true));
    runSelect.addEventListener("change", () => {
      state.selected = runSelect.value;
      refreshRun();
    });
    specFile.addEventListener("change", loadSpecFile);
    startForm.addEventListener("submit", startRun);

    function clsStatus(status) {
      if (status === "pass") return "good";
      if (status === "forced" || status === "running") return "warn";
      if (status === "fail") return "bad";
      return "info";
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[c]));
    }

    function fmt(value, fallback = "-") {
      if (value === null || value === undefined || value === "") return fallback;
      return String(value);
    }

    async function getJson(url) {
      const res = await fetch(url, { cache: "no-store" });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return await res.json();
    }

    async function postJson(url, payload) {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
      return data;
    }

    async function loadSpecFile() {
      const file = specFile.files && specFile.files[0];
      if (!file) return;
      startStatus.textContent = `Loaded ${file.name}`;
      specText.value = await file.text();
    }

    async function startRun(event) {
      event.preventDefault();
      const text = specText.value.trim();
      if (!text) {
        startStatus.textContent = "Enter a requirement or choose a Markdown file first.";
        return;
      }
      const file = specFile.files && specFile.files[0];
      const payload = {
        specText: text,
        filename: file ? file.name : "dashboard_requirement.md",
        llmProvider: document.getElementById("llmProvider").value
      };
      startBtn.disabled = true;
      startStatus.textContent = "Starting agent run...";
      try {
        const result = await postJson("/api/start", payload);
        startStatus.textContent = `Started PID ${result.pid}: ${result.artifact_dir}`;
        state.selected = result.artifact_dir_name;
        await refreshAll(true);
      } catch (err) {
        startStatus.textContent = `Start failed: ${err.message}`;
      } finally {
        startBtn.disabled = false;
      }
    }

    async function refreshAll(forceLatest = false) {
      try {
        const data = await getJson("/api/runs");
        state.runs = data.runs || [];
        renderRunSelect(forceLatest);
        await refreshRun();
      } catch (err) {
        subtitle.textContent = `Dashboard error: ${err.message}`;
      }
    }

    function renderRunSelect(forceLatest) {
      const previous = state.selected;
      runSelect.innerHTML = "";
      for (const run of state.runs) {
        const option = document.createElement("option");
        option.value = run.name;
        option.textContent = `${run.name} (${run.status})`;
        runSelect.appendChild(option);
      }
      if (!state.runs.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No output directories found";
        runSelect.appendChild(option);
        state.selected = "";
        return;
      }
      if (forceLatest || !previous || !state.runs.some(r => r.name === previous)) {
        state.selected = state.runs[0].name;
      } else {
        state.selected = previous;
      }
      runSelect.value = state.selected;
    }

    async function refreshRun() {
      if (!state.selected) {
        renderEmpty();
        return;
      }
      const run = await getJson(`/api/run?dir=${encodeURIComponent(state.selected)}`);
      state.lastRun = run;
      renderRun(run);
    }

    function renderEmpty() {
      subtitle.textContent = "No output directories found.";
      metrics.innerHTML = "";
      pipeline.innerHTML = "";
      reports.innerHTML = "";
      artifactList.innerHTML = '<div class="empty">No artifacts.</div>';
      failedList.innerHTML = '<div class="empty">No failed attempts.</div>';
      latestReport.textContent = "No run selected.";
    }

    function renderRun(run) {
      const active = run.active ? "active" : "idle";
      subtitle.textContent = `${run.path} | ${active} | updated ${fmt(run.updated_at, "unknown")}`;
      const statusClass = clsStatus(run.status_code || "pending");
      metrics.innerHTML = [
        metric("Run Status", `<span class="pill ${statusClass}">${escapeHtml(run.status)}</span>`, run.run_id || run.name),
        metric("Task Progress", `${run.current_task_index}/${run.manager_task_count}`, run.active_task_title || run.active_task_id || "No active task"),
        metric("Artifacts", String(run.artifact_count), `${run.failed_count} failed attempt files`),
        metric("Last Artifact", escapeHtml(run.last_artifact || "-"), run.last_artifact_age || "")
      ].join("");
      pipeline.innerHTML = (run.stages || []).map(stageHtml).join("");
      renderReports(run);
      latestReport.textContent = run.latest_report || "No report yet.";
      artifactList.innerHTML = tableFor(run.recent_artifacts || []);
      failedList.innerHTML = tableFor(run.failed_attempts || []);
      bindFileLinks();
    }

    function metric(label, value, sub) {
      return `<div class="panel metric"><div class="label">${escapeHtml(label)}</div><div class="value">${value}</div><div class="sub">${escapeHtml(sub)}</div></div>`;
    }

    function stageHtml(stage) {
      const count = Number(stage.retry_count || 0);
      const limit = Number(stage.retry_limit || 0);
      const pct = limit > 0 ? Math.min(100, Math.round((count / limit) * 100)) : (count > 0 ? 100 : 0);
      return `<div class="stage ${escapeHtml(stage.status_code)}">
        <div class="stage-title">${escapeHtml(stage.label)}</div>
        <span class="pill ${clsStatus(stage.status_code)}">${escapeHtml(stage.status)}</span>
        <div class="stage-meta">retry ${count}${limit ? " / " + limit : ""}</div>
        <div class="bar"><span style="width:${pct}%"></span></div>
      </div>`;
    }

    function renderReports(run) {
      const entries = Object.entries(run.last_reports || {});
      if (!entries.length) {
        reports.innerHTML = '<div class="empty">No reports yet.</div>';
        return;
      }
      reports.innerHTML = entries.map(([name, text]) => `<div class="report-item"><h3>${escapeHtml(name)}</h3><p>${escapeHtml(text || "-")}</p></div>`).join("");
    }

    function tableFor(items) {
      if (!items.length) return '<div class="empty">No files.</div>';
      const rows = items.map(item => `<tr>
        <td class="path"><a class="file-link" href="#" data-path="${escapeHtml(item.path)}">${escapeHtml(item.path)}</a></td>
        <td>${escapeHtml(item.size_display)}</td>
        <td>${escapeHtml(item.mtime_display)}</td>
      </tr>`).join("");
      return `<table><thead><tr><th>Path</th><th>Size</th><th>Modified</th></tr></thead><tbody>${rows}</tbody></table>`;
    }

    function bindFileLinks() {
      document.querySelectorAll("a.file-link").forEach(link => {
        link.addEventListener("click", async event => {
          event.preventDefault();
          const path = link.getAttribute("data-path");
          try {
            const res = await fetch(`/api/file?dir=${encodeURIComponent(state.selected)}&path=${encodeURIComponent(path)}`, { cache: "no-store" });
            if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
            filePreview.textContent = await res.text();
          } catch (err) {
            filePreview.textContent = `Could not load ${path}: ${err.message}`;
          }
        });
      });
    }

    refreshAll(true);
    state.timer = setInterval(() => refreshAll(false), 2000);
  </script>
</body>
</html>
"""


STAGES = [
    ("architecture", "Architecture", "architecture_review", "architecture_review_forced_forward", "architecture"),
    ("supervisor", "Supervisor", "supervisor_review", "supervisor_review_forced_forward", "supervisor"),
    ("control_datapath", "Control/Data Path", "control_datapath_review", "control_datapath_review_forced_forward", "control_datapath"),
    ("coding", "Coding", "coding_generation", "coding_review_forced_forward", "coding"),
    ("microarchitecture", "Microarchitecture", "microarchitecture_review", "microarchitecture_review_forced_forward", "microarchitecture"),
    ("verification", "Verification", "verification", "verification_review_forced_forward", "verification"),
    ("final_lint", "Final Lint", "final_lint", "final_lint_forced_forward", "testbench"),
    ("human_approval", "Human Approval", "human_approval", "", ""),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Verilog Coding Agent output directories.")
    parser.add_argument("--host", default="127.0.0.1", help="Dashboard bind host.")
    parser.add_argument("--port", type=int, default=8766, help="Dashboard port.")
    parser.add_argument(
        "--root",
        default=str(ROOT),
        help="Repository root containing output_* artifact directories.",
    )
    return parser.parse_args()


def json_read(path: Path, fallback=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def text_read(path: Path, limit: int = MAX_FILE_PREVIEW_BYTES) -> str:
    try:
        data = path.read_bytes()[:limit]
    except OSError as exc:
        return f"Could not read file: {exc}"
    return data.decode("utf-8", errors="replace")


def iso_from_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return ""


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def safe_name(name: str) -> str:
    decoded = unquote(str(name or "")).strip()
    if not decoded:
        return ""
    if decoded.startswith("/") or "\\" in decoded:
        raise ValueError("absolute paths and backslashes are not allowed")
    parts = Path(decoded).parts
    if any(part in {"..", ""} for part in parts):
        raise ValueError("path traversal is not allowed")
    return decoded


def safe_run_dir(root: Path, name: str) -> Path:
    clean = safe_name(name)
    path = (root / clean).resolve()
    path.relative_to(root.resolve())
    if not path.is_dir():
        raise FileNotFoundError(clean)
    return path


def discover_runs(root: Path) -> list[Path]:
    candidates = []
    for path in root.iterdir():
        if not path.is_dir():
            continue
        if path.name.startswith("output_") or path.name == "generated_rtl":
            candidates.append(path)
    return sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)


def run_status(run_dir: Path, summary: dict | None, heartbeat: dict | None) -> tuple[str, str, bool]:
    status = ""
    code = "pending"
    if summary:
        status = str(summary.get("run_status") or "")
        if status == "passed":
            code = "pass"
        elif status == "failed":
            code = "fail"
        elif status:
            code = "pending"
    if not status:
        failed_files = list((run_dir / "failed_attempts").glob("*")) if (run_dir / "failed_attempts").is_dir() else []
        if failed_files:
            status = "running with failures"
            code = "running"
        elif (run_dir / "execution_config.json").exists():
            status = "running"
            code = "running"
        else:
            status = "incomplete"
    active = heartbeat_is_recent(heartbeat)
    if active and code == "pending":
        code = "running"
    return status, code, active


def heartbeat_is_recent(heartbeat: dict | None) -> bool:
    if not heartbeat:
        return False
    raw = str(heartbeat.get("updated_at") or "")
    try:
        updated = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds() < 45


def list_files(base: Path, subdir: str = "", limit: int = 80) -> list[dict]:
    target = base / subdir if subdir else base
    if not target.exists():
        return []
    files = [path for path in target.rglob("*") if path.is_file()]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    result = []
    for path in files[:limit]:
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = path.relative_to(base).as_posix()
        result.append(
            {
                "path": rel,
                "size": stat.st_size,
                "size_display": human_size(stat.st_size),
                "mtime": stat.st_mtime,
                "mtime_display": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return result


def infer_latest_report(run_dir: Path, summary: dict | None, snapshot: dict | None, artifacts: list[dict]) -> str:
    for source in (snapshot, summary):
        if isinstance(source, dict):
            report = source.get("blocking_report") or source.get("final_lint_report")
            if report:
                return str(report)
            last_reports = source.get("last_reports") or source.get("stage_snapshot", {}).get("last_reports")
            if isinstance(last_reports, dict):
                for key in ("verification", "microarchitecture", "control_datapath", "supervisor", "architecture", "final_lint"):
                    if last_reports.get(key):
                        return str(last_reports[key])
    for item in artifacts:
        if item["path"].endswith((".md", ".txt", ".log")):
            return text_read(run_dir / item["path"], 12000)
    return ""


def artifact_counts(run_dir: Path) -> tuple[int, int]:
    all_count = sum(1 for path in run_dir.rglob("*") if path.is_file())
    failed_dir = run_dir / "failed_attempts"
    failed_count = sum(1 for path in failed_dir.rglob("*") if path.is_file()) if failed_dir.exists() else 0
    return all_count, failed_count


def build_stages(snapshot: dict | None, summary: dict | None, artifacts: list[dict]) -> list[dict]:
    snapshot = snapshot or {}
    summary = summary or {}
    flags = snapshot.get("stage_pass_flags") or summary.get("stage_snapshot", {}).get("stage_pass_flags") or {}
    retry_counts = snapshot.get("retry_counts") or summary.get("retry_counts") or summary.get("stage_snapshot", {}).get("retry_counts") or {}
    retry_limits = snapshot.get("retry_limits") or summary.get("retry_limits") or summary.get("stage_snapshot", {}).get("retry_limits") or {}
    artifact_text = "\n".join(item["path"] for item in artifacts[:120])
    stages = []
    for stage_id, label, pass_key, forced_key, retry_key in STAGES:
        passed = bool(flags.get(pass_key))
        forced = bool(flags.get(forced_key)) if forced_key else False
        count = int(retry_counts.get(retry_key, 0) or 0) if retry_key else 0
        limit = int(retry_limits.get(retry_key, 0) or 0) if retry_key else 0
        active = stage_id in artifact_text.lower().replace("/", "_")
        if passed:
            status, code = "PASS", "pass"
        elif forced:
            status, code = "FORCED", "force"
        elif count > 0:
            status, code = "RETRY/FAIL", "fail"
        elif active:
            status, code = "ACTIVE", "active"
        else:
            status, code = "PENDING", "pending"
        stages.append(
            {
                "id": stage_id,
                "label": label,
                "status": status,
                "status_code": code,
                "retry_count": count,
                "retry_limit": limit,
            }
        )
    return stages


def build_run_summary(root: Path, run_dir: Path) -> dict:
    summary = json_read(run_dir / "run_summary.json", {}) or {}
    snapshot = json_read(run_dir / "run_progress_snapshot.json", {}) or {}
    execution = json_read(run_dir / "execution_config.json", {}) or {}
    llm = json_read(run_dir / "llm_config.json", {}) or execution.get("llm_config", {}) or {}
    heartbeat = json_read(run_dir / "dashboard_heartbeat.json", {}) or {}
    artifacts = list_files(run_dir, "", 100)
    failed = list_files(run_dir, "failed_attempts", 80)
    artifact_count, failed_count = artifact_counts(run_dir)
    status, status_code, active = run_status(run_dir, summary, heartbeat)
    manager_task_count = int(
        snapshot.get("manager_task_count")
        or summary.get("manager_task_count")
        or summary.get("stage_snapshot", {}).get("manager_task_count")
        or 0
    )
    current_task_index = int(
        snapshot.get("current_task_index")
        or summary.get("accepted_task_count")
        or summary.get("stage_snapshot", {}).get("current_task_index")
        or 0
    )
    last_artifact = heartbeat.get("last_artifact") or (artifacts[0]["path"] if artifacts else "")
    last_reports = snapshot.get("last_reports") or summary.get("stage_snapshot", {}).get("last_reports") or {}
    return {
        "name": run_dir.name,
        "path": str(run_dir.relative_to(root)),
        "status": status,
        "status_code": status_code,
        "active": active,
        "run_id": summary.get("run_id") or execution.get("run_id") or "",
        "updated_at": heartbeat.get("updated_at") or iso_from_mtime(run_dir),
        "artifact_count": artifact_count,
        "failed_count": failed_count,
        "current_task_index": current_task_index,
        "manager_task_count": manager_task_count,
        "active_task_id": snapshot.get("active_task_id") or summary.get("stage_snapshot", {}).get("active_task_id") or "",
        "active_task_title": snapshot.get("active_task_title") or summary.get("stage_snapshot", {}).get("active_task_title") or "",
        "last_artifact": last_artifact,
        "last_artifact_age": "",
        "llm": llm,
        "execution": execution,
        "stages": build_stages(snapshot, summary, artifacts),
        "last_reports": last_reports,
        "latest_report": infer_latest_report(run_dir, summary, snapshot, failed or artifacts),
        "recent_artifacts": artifacts,
        "failed_attempts": failed,
    }


def slugify_keyword(text: str, fallback: str = "dashboard_run") -> str:
    words = re.findall(r"[A-Za-z0-9가-힣_]+", text)
    selected = []
    for word in words[:4]:
        clean = re.sub(r"[^A-Za-z0-9가-힣_]+", "_", word).strip("_").lower()
        if clean:
            selected.append(clean)
    slug = "_".join(selected) or fallback
    return slug[:48]


def int_option(value: object, default: int, minimum: int = 0, maximum: int = 999) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def env_int(names: tuple[str, ...], default: int, minimum: int = 0, maximum: int = 999) -> int:
    for name in names:
        value = os.getenv(name)
        if value not in {None, ""}:
            return int_option(value, default, minimum, maximum)
    return default


def bool_option(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def env_bool(names: tuple[str, ...], default: bool = False) -> bool:
    for name in names:
        value = os.getenv(name)
        if value is None or value == "":
            continue
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def clean_filename(filename: str) -> str:
    name = Path(str(filename or "dashboard_requirement.md")).name
    name = re.sub(r"[^A-Za-z0-9가-힣_.-]+", "_", name).strip("._")
    if not name:
        name = "dashboard_requirement.md"
    if Path(name).suffix.lower() not in {".md", ".markdown", ".txt"}:
        name += ".md"
    return name[:120]


def unique_artifact_dir(root: Path, spec_text: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = root / f"output_{slugify_keyword(spec_text)}_{timestamp}"
    candidate = base
    index = 2
    while candidate.exists():
        candidate = root / f"{base.name}_{index}"
        index += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def redacted_command(command: list[str], env_updates: dict[str, str]) -> list[str]:
    redacted = list(command)
    if env_updates.get("LLM_API_KEY"):
        redacted.append("[api-key passed by environment]")
    return redacted


def start_agent_run(root: Path, payload: dict) -> dict:
    spec_text = str(payload.get("specText") or "").strip()
    if not spec_text:
        raise ValueError("Requirement text is empty.")
    if len(spec_text) > MAX_SPEC_CHARS:
        raise ValueError(f"Requirement is too large. Maximum is {MAX_SPEC_CHARS} characters.")

    artifact_dir = unique_artifact_dir(root, spec_text)
    spec_filename = clean_filename(str(payload.get("filename") or "dashboard_requirement.md"))
    spec_path = artifact_dir / spec_filename
    spec_path.write_text(spec_text, encoding="utf-8")

    max_retries = env_int(("DASHBOARD_MAX_RETRIES", "MAX_RETRIES"), 10, 0, 999)
    max_tasks = env_int(("DASHBOARD_MAX_MANAGER_TASKS", "MAX_MANAGER_TASKS"), 32, 1, 128)
    command = [
        sys.executable,
        str(root / "main.py"),
        "--spec",
        str(spec_path),
        "--artifact-dir",
        str(artifact_dir),
        "--max-retries",
        str(max_retries),
        "--max-architecture-retries",
        str(max_retries),
        "--max-supervisor-retries",
        str(max_retries),
        "--max-control-datapath-retries",
        str(max_retries),
        "--max-testbench-retries",
        str(max_retries),
        "--max-manager-tasks",
        str(max_tasks),
    ]

    auto_approve = env_bool(("DASHBOARD_AUTO_APPROVE", "AUTO_APPROVE_FINAL"), True)
    if auto_approve:
        command.append("--auto-approve")
    no_testbench = env_bool(("DASHBOARD_NO_TESTBENCH", "NO_TESTBENCH"), False)
    require_lint = env_bool(("DASHBOARD_REQUIRE_LINT", "REQUIRE_LINT"), False)
    if no_testbench:
        command.append("--no-testbench")
    if require_lint:
        command.append("--require-lint")

    llm_provider = str(payload.get("llmProvider") or "").strip()
    if llm_provider:
        command.extend(["--llm-provider", llm_provider])

    env = os.environ.copy()

    stdout_path = artifact_dir / "dashboard_stdout.log"
    stdout_handle = stdout_path.open("ab")
    process = subprocess.Popen(
        command,
        cwd=str(root),
        env=env,
        stdout=stdout_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    stdout_handle.close()

    job = {
        "pid": process.pid,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "artifact_dir": artifact_dir.name,
        "artifact_dir_path": str(artifact_dir),
        "spec_file": spec_path.name,
        "stdout_log": stdout_path.name,
        "command": redacted_command(command, {}),
        "options": {
            "auto_approve": auto_approve,
            "no_testbench": no_testbench,
            "require_lint": require_lint,
            "max_retries": max_retries,
            "max_manager_tasks": max_tasks,
            "llm_provider": str(payload.get("llmProvider") or ""),
        },
    }
    (artifact_dir / "dashboard_job.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "pid": process.pid,
        "artifact_dir": artifact_dir.name,
        "artifact_dir_name": artifact_dir.name,
        "spec_file": spec_path.name,
        "stdout_log": stdout_path.name,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    root = ROOT

    def log_message(self, fmt: str, *args):  # noqa: A003
        return

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK):
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def send_text(self, text: str, content_type: str = "text/plain; charset=utf-8"):
        encoded = text.encode("utf-8")
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_text(HTML, "text/html; charset=utf-8")
            elif parsed.path == "/api/runs":
                self.handle_runs()
            elif parsed.path == "/api/run":
                self.handle_run(parsed.query)
            elif parsed.path == "/api/file":
                self.handle_file(parsed.query)
            else:
                self.send_error(HTTPStatus.NOT_FOUND.value, "Not found")
        except (FileNotFoundError, ValueError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except OSError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/start":
                self.handle_start()
            else:
                self.send_error(HTTPStatus.NOT_FOUND.value, "Not found")
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except OSError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            raise ValueError("Request body is empty.")
        if length > MAX_START_PAYLOAD_BYTES:
            raise ValueError(f"Request body is too large. Maximum is {MAX_START_PAYLOAD_BYTES} bytes.")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        return data

    def handle_start(self):
        payload = self.read_json_body()
        result = start_agent_run(self.root, payload)
        self.send_json(result, HTTPStatus.CREATED)

    def handle_runs(self):
        runs = []
        for run_dir in discover_runs(self.root):
            summary = json_read(run_dir / "run_summary.json", {}) or {}
            heartbeat = json_read(run_dir / "dashboard_heartbeat.json", {}) or {}
            status, status_code, active = run_status(run_dir, summary, heartbeat)
            runs.append(
                {
                    "name": run_dir.name,
                    "path": str(run_dir.relative_to(self.root)),
                    "status": status,
                    "status_code": status_code,
                    "active": active,
                    "updated_at": heartbeat.get("updated_at") or iso_from_mtime(run_dir),
                }
            )
        self.send_json({"runs": runs})

    def handle_run(self, query: str):
        params = parse_qs(query)
        run_name = params.get("dir", [""])[0]
        run_dir = safe_run_dir(self.root, run_name)
        self.send_json(build_run_summary(self.root, run_dir))

    def handle_file(self, query: str):
        params = parse_qs(query)
        run_name = params.get("dir", [""])[0]
        rel_path = safe_name(params.get("path", [""])[0])
        run_dir = safe_run_dir(self.root, run_name)
        path = (run_dir / rel_path).resolve()
        path.relative_to(run_dir.resolve())
        if not path.is_file():
            raise FileNotFoundError(rel_path)
        content_type = mimetypes.guess_type(path.name)[0] or "text/plain"
        if content_type == "application/json":
            content_type += "; charset=utf-8"
        elif content_type.startswith("text/") or path.suffix in {".md", ".v", ".vh", ".log", ".f"}:
            content_type = "text/plain; charset=utf-8"
        else:
            content_type = "text/plain; charset=utf-8"
        self.send_text(text_read(path), content_type)


def main():
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Dashboard root does not exist: {root}")
    if load_dotenv is not None:
        load_dotenv(root / ".env", override=False)
    DashboardHandler.root = root
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url_host = "localhost" if args.host in {"127.0.0.1", "0.0.0.0"} else args.host
    print(f"Verilog Agent Dashboard: http://{url_host}:{args.port}")
    print(f"Monitoring root: {root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
