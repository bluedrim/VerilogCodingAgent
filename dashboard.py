#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import traceback
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


DASHBOARD_HTML_PATH = ROOT / "dashboard.html"


def load_dashboard_html() -> str:
    return DASHBOARD_HTML_PATH.read_text(encoding="utf-8")


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

RETRY_COUNT_FIELDS = {
    "architecture": ("architecture_retry_count",),
    "supervisor": ("supervisor_retry_count",),
    "control_datapath": ("control_datapath_retry_count",),
    "coding": ("coding_retry_count",),
    "microarchitecture": ("microarchitecture_retry_count",),
    "verification": ("verification_retry_count",),
    "testbench": ("testbench_retry_count",),
}

RETRY_LIMIT_FIELDS = {
    "architecture": ("max_architecture_retries",),
    "supervisor": ("max_supervisor_retries",),
    "control_datapath": ("max_control_datapath_retries",),
    "coding": ("max_retries",),
    "microarchitecture": ("max_retries",),
    "verification": ("max_retries",),
    "testbench": ("max_testbench_retries",),
}

STAGE_REPORT_FIELDS = {
    "architecture": ("architecture_review_report", "last_architecture_review_report"),
    "supervisor": ("supervisor_review_report", "last_supervisor_review_report"),
    "control_datapath": ("control_datapath_review_report",),
    "microarchitecture": ("microarchitecture_report", "last_microarchitecture_report"),
    "verification": ("verification_report", "last_verification_report"),
    "final_lint": ("final_lint_report", "last_lint_report", "lint_report"),
}

STAGE_REPORT_FILE_HINTS = {
    "architecture": ("architecture_review",),
    "supervisor": ("supervisor_review",),
    "control_datapath": ("control_datapath_review",),
    "microarchitecture": ("microarchitecture_review", "microarchitecture_static"),
    "verification": ("verification_report", "verification_repair", "sanity_failed", "lint_failed"),
    "final_lint": ("final_lint_report", "lint_attempt"),
}


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


def pid_is_running(pid: object) -> bool:
    try:
        numeric_pid = int(pid)
    except (TypeError, ValueError):
        return False
    if numeric_pid <= 0:
        return False
    waitpid = getattr(os, "waitpid", None)
    nohang = getattr(os, "WNOHANG", None)
    if callable(waitpid) and nohang is not None:
        try:
            waited_pid, _ = waitpid(numeric_pid, nohang)
            if waited_pid == numeric_pid:
                return False
            return True
        except (ChildProcessError, OSError):
            pass
    try:
        os.kill(numeric_pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def run_process_is_active(run_dir: Path, heartbeat: dict | None) -> bool:
    if heartbeat and heartbeat.get("process_id"):
        return pid_is_running(heartbeat.get("process_id"))
    job = json_read(run_dir / "dashboard_job.json", {}) or {}
    if job.get("pid"):
        return pid_is_running(job.get("pid"))
    return heartbeat_is_recent(heartbeat)


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
    active = run_process_is_active(run_dir, heartbeat)
    if active:
        status = "running"
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


def nested_dict(source: dict | None, *keys: str) -> dict:
    current: object = source or {}
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def first_int(default: int, *values: object) -> int:
    for value in values:
        if value in {None, ""}:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return default


def load_manager_plan(run_dir: Path, snapshot: dict, summary: dict, checkpoint: dict) -> list[dict]:
    checkpoint_state = nested_dict(checkpoint, "state")
    for plan in (
        snapshot.get("manager_plan"),
        summary.get("manager_plan"),
        nested_dict(summary, "stage_snapshot").get("manager_plan"),
        checkpoint_state.get("manager_plan"),
        json_read(run_dir / "manager_plan.json", []),
    ):
        if isinstance(plan, list):
            return [task for task in plan if isinstance(task, dict)]
    return []


def active_manager_task(manager_plan: list[dict], current_task_index: int) -> dict:
    if 0 <= current_task_index < len(manager_plan):
        return manager_plan[current_task_index]
    return {}


def task_text(task: dict, *keys: str) -> str:
    for key in keys:
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and value != "" and value != []:
            return json.dumps(value, ensure_ascii=False)
    return ""


def task_progress_display(current_task_index: int, manager_task_count: int) -> tuple[int, int]:
    if manager_task_count <= 0:
        return 0, 0
    if current_task_index >= manager_task_count:
        return manager_task_count, manager_task_count
    return max(current_task_index + 1, 1), manager_task_count


def first_text_from_fields(sources: list[object], fields: tuple[str, ...]) -> str:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for field in fields:
            value = source.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if value is not None and value != "" and value != []:
                return json.dumps(value, ensure_ascii=False)
    return ""


def first_report_from_maps(stage_key: str, report_maps: list[object]) -> str:
    for report_map in report_maps:
        if isinstance(report_map, dict):
            value = report_map.get(stage_key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def first_report_from_files(run_dir: Path, artifacts: list[dict], stage_key: str) -> str:
    hints = STAGE_REPORT_FILE_HINTS.get(stage_key, ())
    if not hints:
        return ""
    for item in artifacts:
        path = str(item.get("path") or "")
        lower_path = path.lower()
        if not lower_path.endswith((".md", ".txt", ".log")):
            continue
        if any(hint in lower_path for hint in hints):
            return text_read(run_dir / path, 12000).strip()
    return ""


def collect_stage_reports(
    run_dir: Path,
    snapshot: dict,
    summary: dict,
    checkpoint: dict,
    artifacts: list[dict],
) -> dict[str, str]:
    stage_snapshot = nested_dict(summary, "stage_snapshot")
    checkpoint_state = nested_dict(checkpoint, "state")
    report_maps = [
        snapshot.get("last_reports"),
        summary.get("last_reports"),
        stage_snapshot.get("last_reports"),
        checkpoint_state.get("last_reports"),
    ]
    field_sources = [snapshot, summary, stage_snapshot, checkpoint_state]
    reports: dict[str, str] = {}
    for stage_key, fields in STAGE_REPORT_FIELDS.items():
        report = (
            first_report_from_maps(stage_key, report_maps)
            or first_text_from_fields(field_sources, fields)
            or first_report_from_files(run_dir, artifacts, stage_key)
        )
        if report:
            reports[stage_key] = report
    blocking_report = first_text_from_fields(field_sources, ("blocking_report",))
    if blocking_report and not reports.get("blocking"):
        reports["blocking"] = blocking_report
    return reports


def first_stage_int(
    stage_key: str,
    map_sources: list[object],
    flat_sources: list[object],
    flat_fields: tuple[str, ...],
    default: int = 0,
) -> int:
    for source in map_sources:
        if isinstance(source, dict) and stage_key in source:
            return first_int(default, source.get(stage_key))
    for source in flat_sources:
        if not isinstance(source, dict):
            continue
        for field in flat_fields:
            if field in source:
                return first_int(default, source.get(field))
    return default


def build_stages(
    snapshot: dict | None,
    summary: dict | None,
    artifacts: list[dict],
    checkpoint: dict | None = None,
    execution: dict | None = None,
    job: dict | None = None,
) -> list[dict]:
    snapshot = snapshot or {}
    summary = summary or {}
    checkpoint = checkpoint or {}
    execution = execution or {}
    job = job or {}
    stage_snapshot = nested_dict(summary, "stage_snapshot")
    checkpoint_state = nested_dict(checkpoint, "state")
    job_options = nested_dict(job, "options")
    flags = (
        snapshot.get("stage_pass_flags")
        or stage_snapshot.get("stage_pass_flags")
        or summary.get("stage_pass_flags")
        or {}
    )
    retry_count_maps = [
        snapshot.get("retry_counts"),
        summary.get("retry_counts"),
        stage_snapshot.get("retry_counts"),
        checkpoint_state.get("retry_counts"),
    ]
    retry_limit_maps = [
        snapshot.get("retry_limits"),
        summary.get("retry_limits"),
        stage_snapshot.get("retry_limits"),
        checkpoint_state.get("retry_limits"),
        execution.get("retry_limits"),
    ]
    retry_count_sources = [snapshot, summary, stage_snapshot, checkpoint_state]
    retry_limit_sources = [snapshot, summary, stage_snapshot, checkpoint_state, execution, job_options]
    artifact_text = "\n".join(item["path"] for item in artifacts[:120])
    stages = []
    for stage_id, label, pass_key, forced_key, retry_key in STAGES:
        passed = bool(flags.get(pass_key))
        forced = bool(flags.get(forced_key)) if forced_key else False
        count = (
            first_stage_int(
                retry_key,
                retry_count_maps,
                retry_count_sources,
                RETRY_COUNT_FIELDS.get(retry_key, ()),
            )
            if retry_key
            else 0
        )
        limit = (
            first_stage_int(
                retry_key,
                retry_limit_maps,
                retry_limit_sources,
                RETRY_LIMIT_FIELDS.get(retry_key, ()),
            )
            if retry_key
            else 0
        )
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
    checkpoint = json_read(run_dir / "run_state_checkpoint.json", {}) or {}
    execution = json_read(run_dir / "execution_config.json", {}) or {}
    job = json_read(run_dir / "dashboard_job.json", {}) or {}
    llm = json_read(run_dir / "llm_config.json", {}) or execution.get("llm_config", {}) or {}
    heartbeat = json_read(run_dir / "dashboard_heartbeat.json", {}) or {}
    artifacts = list_files(run_dir, "", 100)
    failed = list_files(run_dir, "failed_attempts", 80)
    artifact_count, failed_count = artifact_counts(run_dir)
    status, status_code, active = run_status(run_dir, summary, heartbeat)
    resume_stage = str(checkpoint.get("resume_stage") or "")
    can_continue = bool(checkpoint and resume_stage and not active)
    if active:
        continue_reason = "This run is already active."
    elif not checkpoint:
        continue_reason = "No continuation checkpoint is available for this run."
    elif not resume_stage:
        continue_reason = "This run is complete and has no pending stage."
    else:
        continue_reason = ""
    stage_snapshot = nested_dict(summary, "stage_snapshot")
    checkpoint_state = nested_dict(checkpoint, "state")
    manager_plan = load_manager_plan(run_dir, snapshot, summary, checkpoint)
    manager_task_count = first_int(
        len(manager_plan),
        snapshot.get("manager_task_count"),
        summary.get("manager_task_count"),
        stage_snapshot.get("manager_task_count"),
    )
    current_task_index = first_int(
        0,
        snapshot.get("current_task_index"),
        stage_snapshot.get("current_task_index"),
        checkpoint_state.get("current_task_index"),
        summary.get("accepted_task_count"),
    )
    progress_current, progress_total = task_progress_display(current_task_index, manager_task_count)
    active_task = active_manager_task(manager_plan, current_task_index)
    active_task_id = (
        snapshot.get("active_task_id")
        or stage_snapshot.get("active_task_id")
        or task_text(active_task, "id")
    )
    active_task_title = (
        snapshot.get("active_task_title")
        or stage_snapshot.get("active_task_title")
        or task_text(active_task, "title")
    )
    active_task_goal = task_text(
        active_task,
        "goal",
        "objective",
        "description",
        "summary",
        "deliverable",
        "title",
    )
    last_artifact = heartbeat.get("last_artifact") or (artifacts[0]["path"] if artifacts else "")
    last_reports = collect_stage_reports(run_dir, snapshot, summary, checkpoint, artifacts)
    return {
        "name": run_dir.name,
        "path": str(run_dir.relative_to(root)),
        "status": status,
        "status_code": status_code,
        "active": active,
        "can_continue": can_continue,
        "continue_reason": continue_reason,
        "resume_stage": resume_stage,
        "checkpoint_phase": checkpoint.get("phase") or "",
        "checkpoint_saved_at": checkpoint.get("saved_at") or "",
        "run_id": summary.get("run_id") or execution.get("run_id") or "",
        "updated_at": heartbeat.get("updated_at") or iso_from_mtime(run_dir),
        "artifact_count": artifact_count,
        "failed_count": failed_count,
        "current_task_index": current_task_index,
        "manager_task_count": manager_task_count,
        "task_progress_current": progress_current,
        "task_progress_total": progress_total,
        "active_task_id": active_task_id,
        "active_task_title": active_task_title,
        "active_task_goal": active_task_goal,
        "last_artifact": last_artifact,
        "last_artifact_age": "",
        "llm": llm,
        "execution": execution,
        "stages": build_stages(snapshot, summary, artifacts, checkpoint, execution, job),
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


def append_dashboard_error(root: Path, exc: BaseException) -> None:
    try:
        log_path = root / "dashboard_errors.log"
        timestamp = datetime.now(timezone.utc).isoformat()
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{timestamp}] {detail}\n")
    except OSError:
        return


def launch_agent_process(command: list[str], root: Path, stdout_path: Path, env: dict[str, str]) -> subprocess.Popen:
    stdout_handle = stdout_path.open("ab")
    try:
        return subprocess.Popen(
            command,
            cwd=str(root),
            env=env,
            stdout=stdout_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        stdout_handle.close()


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

    max_retries = env_int(("DASHBOARD_MAX_RETRIES", "MAX_RETRIES"), 3, 0, 999)
    max_architecture_retries = env_int(
        ("DASHBOARD_MAX_ARCHITECTURE_RETRIES", "MAX_ARCHITECTURE_RETRIES"),
        max_retries,
        0,
        999,
    )
    max_supervisor_retries = env_int(
        ("DASHBOARD_MAX_SUPERVISOR_RETRIES", "MAX_SUPERVISOR_RETRIES"),
        max_retries,
        0,
        999,
    )
    max_control_datapath_retries = env_int(
        ("DASHBOARD_MAX_CONTROL_DATAPATH_RETRIES", "MAX_CONTROL_DATAPATH_RETRIES"),
        max_retries,
        0,
        999,
    )
    max_testbench_retries = env_int(
        ("DASHBOARD_MAX_TESTBENCH_RETRIES", "MAX_TESTBENCH_RETRIES"),
        max_retries,
        0,
        999,
    )
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
        str(max_architecture_retries),
        "--max-supervisor-retries",
        str(max_supervisor_retries),
        "--max-control-datapath-retries",
        str(max_control_datapath_retries),
        "--max-testbench-retries",
        str(max_testbench_retries),
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
    process = launch_agent_process(command, root, stdout_path, env)

    job = {
        "pid": process.pid,
        "created_at": datetime.now(timezone.utc).isoformat(),
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
            "max_architecture_retries": max_architecture_retries,
            "max_supervisor_retries": max_supervisor_retries,
            "max_control_datapath_retries": max_control_datapath_retries,
            "max_testbench_retries": max_testbench_retries,
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


def continue_agent_run(root: Path, payload: dict) -> dict:
    run_name = str(payload.get("dir") or "").strip()
    run_dir = safe_run_dir(root, run_name)
    checkpoint = json_read(run_dir / "run_state_checkpoint.json", {}) or {}
    if not checkpoint:
        raise ValueError("The selected run has no continuation checkpoint.")
    resume_stage = str(checkpoint.get("resume_stage") or "")
    if not resume_stage:
        raise ValueError("The selected run is complete and has no pending stage.")
    heartbeat = json_read(run_dir / "dashboard_heartbeat.json", {}) or {}
    if run_process_is_active(run_dir, heartbeat):
        raise ValueError("The selected run is already active.")

    max_retries = env_int(("DASHBOARD_MAX_RETRIES", "MAX_RETRIES"), 3, 0, 999)
    max_architecture_retries = env_int(
        ("DASHBOARD_MAX_ARCHITECTURE_RETRIES", "MAX_ARCHITECTURE_RETRIES"),
        max_retries,
        0,
        999,
    )
    max_supervisor_retries = env_int(
        ("DASHBOARD_MAX_SUPERVISOR_RETRIES", "MAX_SUPERVISOR_RETRIES"),
        max_retries,
        0,
        999,
    )
    max_control_datapath_retries = env_int(
        ("DASHBOARD_MAX_CONTROL_DATAPATH_RETRIES", "MAX_CONTROL_DATAPATH_RETRIES"),
        max_retries,
        0,
        999,
    )
    max_testbench_retries = env_int(
        ("DASHBOARD_MAX_TESTBENCH_RETRIES", "MAX_TESTBENCH_RETRIES"),
        max_retries,
        0,
        999,
    )
    max_tasks = env_int(("DASHBOARD_MAX_MANAGER_TASKS", "MAX_MANAGER_TASKS"), 32, 1, 128)
    command = [
        sys.executable,
        str(root / "main.py"),
        "--continue",
        "--artifact-dir",
        str(run_dir),
        "--max-retries",
        str(max_retries),
        "--max-architecture-retries",
        str(max_architecture_retries),
        "--max-supervisor-retries",
        str(max_supervisor_retries),
        "--max-control-datapath-retries",
        str(max_control_datapath_retries),
        "--max-testbench-retries",
        str(max_testbench_retries),
        "--max-manager-tasks",
        str(max_tasks),
    ]

    auto_approve = env_bool(("DASHBOARD_AUTO_APPROVE", "AUTO_APPROVE_FINAL"), True)
    no_testbench = env_bool(("DASHBOARD_NO_TESTBENCH", "NO_TESTBENCH"), False)
    require_lint = env_bool(("DASHBOARD_REQUIRE_LINT", "REQUIRE_LINT"), False)
    if auto_approve:
        command.append("--auto-approve")
    if no_testbench:
        command.append("--no-testbench")
    if require_lint:
        command.append("--require-lint")

    llm_provider = str(payload.get("llmProvider") or "").strip()
    if llm_provider:
        command.extend(["--llm-provider", llm_provider])

    stdout_path = run_dir / "dashboard_stdout.log"
    process = launch_agent_process(command, root, stdout_path, os.environ.copy())

    started_at = datetime.now(timezone.utc).isoformat()
    previous_job = json_read(run_dir / "dashboard_job.json", {}) or {}
    continuation_event = {
        "pid": process.pid,
        "started_at": started_at,
        "resume_stage": resume_stage,
        "command": redacted_command(command, {}),
    }
    continuation_history = list(previous_job.get("continuations", []))
    continuation_history.append(continuation_event)
    job = dict(previous_job)
    job.update(
        {
            "pid": process.pid,
            "created_at": previous_job.get("created_at")
            or previous_job.get("started_at")
            or started_at,
            "started_at": started_at,
            "artifact_dir": run_dir.name,
            "artifact_dir_path": str(run_dir),
            "stdout_log": stdout_path.name,
            "command": redacted_command(command, {}),
            "resume_stage": resume_stage,
            "continuation_count": len(continuation_history),
            "continuations": continuation_history,
            "options": {
                "auto_approve": auto_approve,
                "no_testbench": no_testbench,
                "require_lint": require_lint,
                "max_retries": max_retries,
                "max_architecture_retries": max_architecture_retries,
                "max_supervisor_retries": max_supervisor_retries,
                "max_control_datapath_retries": max_control_datapath_retries,
                "max_testbench_retries": max_testbench_retries,
                "max_manager_tasks": max_tasks,
                "llm_provider": llm_provider,
            },
        }
    )
    (run_dir / "dashboard_job.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "dashboard_heartbeat.json").write_text(
        json.dumps(
            {
                "updated_at": started_at,
                "artifact_dir": str(run_dir),
                "process_id": process.pid,
                "last_artifact": "dashboard_job.json",
                "last_artifact_path": str(run_dir / "dashboard_job.json"),
                "last_artifact_bytes": (run_dir / "dashboard_job.json").stat().st_size,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "pid": process.pid,
        "artifact_dir": run_dir.name,
        "artifact_dir_name": run_dir.name,
        "resume_stage": resume_stage,
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
                self.send_text(load_dashboard_html(), "text/html; charset=utf-8")
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
            elif parsed.path == "/api/continue":
                self.handle_continue()
            else:
                self.send_error(HTTPStatus.NOT_FOUND.value, "Not found")
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except OSError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except Exception as exc:  # pragma: no cover - defensive server guard
            append_dashboard_error(self.root, exc)
            self.send_json(
                {"error": f"Dashboard request failed without stopping the server: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

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

    def handle_continue(self):
        payload = self.read_json_body()
        result = continue_agent_run(self.root, payload)
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
