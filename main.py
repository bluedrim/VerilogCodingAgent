import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Dict, List, Optional, TypedDict


def positive_int(raw_value: str) -> int:
    value = int(raw_value)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be 0 or greater")
    return value


def bounded_temperature(raw_value: str) -> float:
    value = float(raw_value)
    if value < 0 or value > 2:
        raise argparse.ArgumentTypeError("temperature must be between 0 and 2")
    return value


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Verilog coding agent.")
    parser.add_argument("--spec", help="RTL requirement text or path to a requirement file.")
    parser.add_argument(
        "--max-retries",
        type=positive_int,
        default=3,
        help="Maximum retries per coding, microarchitecture review, and verification stage.",
    )
    parser.add_argument(
        "--max-architecture-retries",
        type=positive_int,
        default=2,
        help="Maximum architecture review retries.",
    )
    parser.add_argument(
        "--max-supervisor-retries",
        type=positive_int,
        default=2,
        help="Maximum Supervisor review retries.",
    )
    parser.add_argument(
        "--max-control-datapath-retries",
        type=positive_int,
        default=2,
        help="Maximum Control/Data Path plan review retries.",
    )
    parser.add_argument(
        "--max-testbench-retries",
        type=positive_int,
        default=2,
        help="Maximum smoke testbench generation retries.",
    )
    parser.add_argument(
        "--artifact-dir",
        default=None,
        help=(
            "Directory for generated artifacts and logs. "
            "Defaults to output_<project_keyword>_<YYYYMMDD>_<HHMMSS>."
        ),
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip final interactive approval and write generated files.",
    )
    parser.add_argument(
        "--no-testbench",
        action="store_true",
        help="Skip smoke testbench generation.",
    )
    parser.add_argument(
        "--require-lint",
        action="store_true",
        help="Fail when neither verilator nor iverilog is installed.",
    )
    parser.add_argument(
        "--lint-timeout",
        type=positive_int,
        default=30,
        help="Syntax lint timeout in seconds.",
    )
    parser.add_argument(
        "--allow-blackboxes",
        action="store_true",
        help="Allow unresolved module instantiations in static sanity checks.",
    )
    parser.add_argument(
        "--max-generated-file-bytes",
        type=positive_int,
        default=500_000,
        help="Maximum allowed bytes per generated RTL/testbench file.",
    )
    parser.add_argument(
        "--max-generated-files",
        type=positive_int,
        default=64,
        help="Maximum number of files accepted from RTL/testbench generation.",
    )
    parser.add_argument(
        "--max-context-chars",
        type=positive_int,
        default=120_000,
        help="Maximum characters of RTL/context text sent to each LLM prompt.",
    )
    parser.add_argument(
        "--max-user-request-chars",
        type=positive_int,
        default=200_000,
        help="Maximum accepted characters for the user requirement.",
    )
    parser.add_argument(
        "--max-manager-tasks",
        type=positive_int,
        default=32,
        help="Maximum number of Manager tasks accepted from the planning step.",
    )
    parser.add_argument(
        "--llm-provider",
        choices=["ollama", "gpt-oss", "openai"],
        help="LLM provider/backend. 'openai' uses the current account API key from OPENAI_API_KEY or --llm-api-key.",
    )
    parser.add_argument("--llm-model", help="LLM model name, for example gpt-oss:20b or gpt-4.1.")
    parser.add_argument("--llm-temperature", type=bounded_temperature, help="LLM temperature.")
    parser.add_argument(
        "--llm-api-url",
        help="OpenAI-compatible chat completions URL, for example http://abc.net:30001/chat/completions.",
    )
    parser.add_argument("--llm-api-key", help="API key for OpenAI or an OpenAI-compatible endpoint.")
    parser.add_argument(
        "--fail-on-manager-fallback",
        action="store_true",
        help="Fail instead of using the single-task fallback when Manager planning output is invalid.",
    )
    return parser


if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
    build_arg_parser().parse_args()
    sys.exit(0)

try:
    from dotenv import load_dotenv
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_ollama.chat_models import ChatOllama
    from langgraph.graph import END, StateGraph
    from langgraph.prebuilt import ToolNode

    from tools import write_verilog_file
except ModuleNotFoundError as exc:
    print(f"Missing dependency: {exc.name}")
    print("Install project dependencies with: python3 -m pip install -r requirements.txt")
    sys.exit(1)


# --- 1. Load Environment Variables ---
load_dotenv()


# --- 2. Define Agent State ---
class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], lambda x, y: x + y]
    user_request: str
    manager_plan: List[Dict[str, str]]
    architecture_contract: str
    architecture_review_passed: bool
    architecture_review_report: str
    architecture_retry_count: int
    max_architecture_retries: int
    current_task_index: int
    supervisor_plan: str
    supervisor_review_passed: bool
    supervisor_review_report: str
    supervisor_retry_count: int
    max_supervisor_retries: int
    control_datapath_plan: str
    control_datapath_review_passed: bool
    control_datapath_review_report: str
    control_datapath_retry_count: int
    max_control_datapath_retries: int
    microarchitecture_passed: bool
    microarchitecture_report: str
    coding_retry_count: int
    microarchitecture_retry_count: int
    verification_retry_count: int
    testbench_retry_count: int
    max_testbench_retries: int
    rtl_context: str
    candidate_files: List[Dict[str, str]]
    final_files: List[Dict[str, str]]
    testbench_files: List[Dict[str, str]]
    top_module_candidates: List[str]
    verification_passed: bool
    verification_report: str
    lint_report: str
    require_lint: bool
    lint_timeout_seconds: int
    allow_blackboxes: bool
    final_lint_passed: bool
    final_lint_report: str
    human_approved: bool
    skip_testbench: bool
    max_retries: int
    run_status: str
    failed_stage: str
    blocking_report: str
    max_generated_file_bytes: int
    max_generated_files: int
    max_context_chars: int
    max_user_request_chars: int
    max_manager_tasks: int
    manager_fallback_used: bool
    fail_on_manager_fallback: bool
    run_id: str
    generation_ok: bool
    error_message: str


# --- 3. Setup LLM and Tools ---
def normalize_chat_completions_url(url: str | None) -> str:
    if not url:
        return ""
    normalized = url.rstrip("/")
    suffix = "/chat/completions"
    if normalized.endswith(suffix):
        normalized = normalized[: -len(suffix)]
    return normalized


def resolve_llm_settings(
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
) -> Dict[str, object]:
    resolved_provider = (provider or os.getenv("LLM_PROVIDER") or "ollama").strip().lower()
    if resolved_provider not in {"ollama", "gpt-oss", "openai"}:
        raise ValueError("Unsupported LLM provider. Use ollama, gpt-oss, or openai.")
    resolved_temperature = (
        temperature if temperature is not None else float(os.getenv("LLM_TEMPERATURE", "0.1"))
    )
    resolved_api_url = api_url or os.getenv("GPT_OSS_API_URL") or os.getenv("LLM_API_URL") or ""
    resolved_api_key = (
        api_key
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("GPT_OSS_API_KEY")
        or os.getenv("LLM_API_KEY")
        or ""
    )

    if resolved_provider == "gpt-oss":
        resolved_model = model or os.getenv("GPT_OSS_MODEL") or os.getenv("LLM_MODEL") or "gpt-oss"
        backend = "openai-compatible" if resolved_api_url else "ollama"
    elif resolved_provider == "openai":
        resolved_model = model or os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL") or "gpt-4.1"
        backend = "openai"
    else:
        resolved_model = model or os.getenv("LLM_MODEL") or os.getenv("OLLAMA_MODEL") or "gpt-oss:20b"
        backend = resolved_provider

    if backend == "openai-compatible":
        base_url = normalize_chat_completions_url(resolved_api_url)
    elif backend == "openai":
        base_url = normalize_chat_completions_url(resolved_api_url)
    elif backend == "ollama":
        base_url = os.getenv("OLLAMA_BASE_URL", "")
    else:
        base_url = resolved_api_url

    return {
        "provider": resolved_provider,
        "backend": backend,
        "model": resolved_model,
        "temperature": resolved_temperature,
        "api_url": resolved_api_url,
        "base_url": base_url,
        "api_key": resolved_api_key,
    }


def public_llm_config(settings: Dict[str, object]) -> Dict[str, object]:
    api_key = str(settings.get("api_key") or "")
    redacted_key = ""
    if api_key:
        redacted_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "***"
    return {
        "provider": settings.get("provider", ""),
        "backend": settings.get("backend", ""),
        "model": settings.get("model", ""),
        "temperature": settings.get("temperature", ""),
        "api_url": settings.get("api_url", ""),
        "base_url": settings.get("base_url", ""),
        "api_key_set": bool(api_key),
        "api_key_redacted": redacted_key,
    }


def discover_lint_tool() -> str:
    lint_tool = shutil.which("verilator") or shutil.which("iverilog")
    return lint_tool or ""


def create_llm(
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
):
    settings = resolve_llm_settings(provider, model, temperature, api_url, api_key)
    backend = str(settings["backend"])
    model_name = str(settings["model"])
    resolved_temperature = float(settings["temperature"])

    if backend in {"openai", "openai-compatible"}:
        try:
            from langchain_openai import ChatOpenAI
        except ModuleNotFoundError as exc:
            print(f"Missing dependency: {exc.name}")
            print("Install project dependencies with: python3 -m pip install -r requirements.txt")
            sys.exit(1)
        if backend == "openai" and not settings["api_key"]:
            raise ValueError("OpenAI provider requires OPENAI_API_KEY or --llm-api-key.")
        kwargs = {
            "model": model_name,
            "temperature": resolved_temperature,
            "api_key": str(settings["api_key"] or "dummy"),
        }
        if settings["base_url"]:
            kwargs["base_url"] = str(settings["base_url"])
        return ChatOpenAI(**kwargs)

    if backend != "ollama":
        raise ValueError("Unsupported LLM backend. Supported backends: ollama, openai, openai-compatible")

    kwargs = {"model": model_name, "temperature": resolved_temperature}
    if settings["base_url"]:
        kwargs["base_url"] = str(settings["base_url"])
    return ChatOllama(**kwargs)


def llm_config(
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
):
    return public_llm_config(resolve_llm_settings(provider, model, temperature, api_url, api_key))


llm = None
active_llm_config = llm_config()
writer_tools = [write_verilog_file]
ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "generated_rtl"))


PROJECT_KEYWORD_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "build",
    "coding",
    "create",
    "design",
    "for",
    "generate",
    "implement",
    "make",
    "module",
    "rtl",
    "systemverilog",
    "that",
    "the",
    "verilog",
    "with",
}


def sanitize_artifact_name(value: object, fallback: str = "item") -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "_", text).strip("._")
    return text or fallback


def derive_project_keyword(requirement: str, fallback: str = "verilog_project") -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", requirement.lower())
    selected = []
    for word in words:
        if word in PROJECT_KEYWORD_STOPWORDS:
            continue
        if word not in selected:
            selected.append(word)
        if len(selected) >= 4:
            break
    return sanitize_artifact_name("_".join(selected), fallback)


def default_output_dir(requirement: str, timestamp: datetime) -> Path:
    keyword = derive_project_keyword(requirement)
    return Path(f"output_{keyword}_{timestamp.strftime('%Y%m%d_%H%M%S')}")


def resolve_artifact_dir(args: argparse.Namespace, requirement: str, timestamp: datetime) -> Path:
    configured_dir = args.artifact_dir or os.getenv("ARTIFACT_DIR")
    if configured_dir:
        return Path(configured_dir)
    return default_output_dir(requirement, timestamp)


def artifact_path(relative_path: str) -> Path:
    candidate = ARTIFACT_DIR / relative_path
    try:
        candidate.resolve().relative_to(ARTIFACT_DIR.resolve())
    except ValueError as exc:
        raise ValueError(f"Artifact path escapes artifact directory: {relative_path}") from exc
    return candidate


def _strip_json_fence(raw_content: str) -> str:
    content = raw_content.strip()
    if content.startswith("```json"):
        content = content[7:].strip()
    elif content.startswith("```"):
        content = content[3:].strip()
    if content.endswith("```"):
        content = content[:-3].strip()
    return content


def _balanced_json_end(content: str, start: int) -> int:
    opener = content[start]
    closer = {"[": "]", "{": "}"}.get(opener)
    if closer is None:
        return -1

    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(content)):
        char = content[idx]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _json_candidates(content: str) -> List[str]:
    candidates = []
    idx = 0
    while idx < len(content):
        if content[idx] in "[{":
            end = _balanced_json_end(content, idx)
            if end != -1:
                candidates.append(content[idx : end + 1])
                idx = end + 1
                continue
        idx += 1
    return candidates


def _extract_json_candidate(raw_content: str) -> str:
    content = _strip_json_fence(raw_content)
    candidates = _json_candidates(content)
    if candidates:
        return candidates[-1]
    return content


def _load_json(raw_content: str):
    content = _strip_json_fence(raw_content)
    try:
        return json.loads(content)
    except json.JSONDecodeError as first_error:
        last_error = first_error

    for candidate in reversed(_json_candidates(content)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    raise last_error


TRUE_REVIEW_VALUES = {
    "true",
    "pass",
    "passed",
    "yes",
    "ok",
    "success",
    "successful",
    "valid",
    "approve",
    "approved",
    "accepted",
}
FALSE_REVIEW_VALUES = {
    "false",
    "fail",
    "failed",
    "no",
    "ng",
    "error",
    "invalid",
    "needs_changes",
    "needs change",
    "request_changes",
    "request changes",
    "reject",
    "rejected",
    "denied",
    "deny",
}
BOOLEAN_FALSE_VALUES = {"false", "no", "0", "none", "null", "n/a"}


def parse_review_result(raw_content: str, invalid_json_report: str):
    try:
        result = _load_json(raw_content)
        return _parse_review_json_result(result)
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        fallback_passed = _parse_review_text_verdict(raw_content)
        report = _compact_review_text(raw_content) or invalid_json_report
        if fallback_passed is None:
            return False, invalid_json_report
        return fallback_passed, report


def _parse_review_json_result(result: object):
    if isinstance(result, list) and len(result) == 1:
        result = result[0]
    if not isinstance(result, dict):
        raise TypeError("Review JSON result must be an object.")

    passed = _review_pass_value(result)
    report = _review_report_value(result)
    return passed, report


def _review_pass_value(result: Dict[str, object]) -> bool:
    for key in ("pass", "passed", "ok", "success", "valid", "approved", "accepted"):
        if key in result:
            return _json_bool(result.get(key))

    for key in ("fail", "failed", "error", "invalid", "rejected"):
        if key in result:
            return _negated_failure_value(result.get(key))

    for key in ("result", "status", "verdict", "decision"):
        if key not in result:
            continue
        value = str(result.get(key, "")).strip().lower()
        if value in TRUE_REVIEW_VALUES:
            return True
        if value in FALSE_REVIEW_VALUES:
            return False
    return False


def _review_report_value(result: Dict[str, object]) -> str:
    for key in ("report", "reason", "feedback", "summary", "message", "details", "findings"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return "\n".join(str(item).strip() for item in value if str(item).strip())
    return json.dumps(result, ensure_ascii=False)


def _parse_review_text_verdict(raw_content: str) -> Optional[bool]:
    lowered = raw_content.strip().lower()
    if re.search(r"\b(fail|failed|reject|rejected|invalid|not\s+pass|does\s+not\s+pass)\b", lowered):
        return False
    if re.search(r"\b(pass|passed|approve|approved|ok|valid|success)\b", lowered):
        return True
    return None


def _compact_review_text(raw_content: str, limit: int = 2000) -> str:
    compact = raw_content.strip()
    if not compact:
        return ""
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "\n... [truncated]"


def _json_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in TRUE_REVIEW_VALUES
    return False


def _negated_failure_value(value: object) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return not bool(value)
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped in BOOLEAN_FALSE_VALUES:
            return True
        if stripped in TRUE_REVIEW_VALUES or stripped in FALSE_REVIEW_VALUES:
            return False
    return False


def validate_plan(plan: object, max_tasks: int = 32):
    if not isinstance(plan, list) or not plan:
        return False, "Manager plan must be a non-empty JSON list."
    if max_tasks and len(plan) > max_tasks:
        return False, f"Manager plan has {len(plan)} tasks, above limit {max_tasks}."
    seen_ids = set()
    for idx, task in enumerate(plan):
        if not isinstance(task, dict):
            return False, f"Task {idx} must be an object."
        for key in ("id", "title", "goal", "deliverable"):
            if key not in task or not isinstance(task[key], str) or not task[key].strip():
                return False, f"Task {idx} must include a non-empty '{key}'."
        task_id = task["id"].strip()
        if task_id in seen_ids:
            return False, f"Task id '{task_id}' is duplicated."
        seen_ids.add(task_id)
    for idx, task in enumerate(plan):
        dependencies = task.get("dependencies")
        if dependencies in (None, "", "TBD"):
            continue
        if isinstance(dependencies, str):
            continue
        if isinstance(dependencies, list):
            dependency_ids = [str(item).strip() for item in dependencies]
        else:
            return False, f"Task {idx} dependencies must be a string or list."
        unknown = [dep for dep in dependency_ids if dep and dep not in seen_ids and dep != task["id"]]
        if unknown:
            return False, f"Task {idx} references unknown dependencies: {', '.join(unknown)}"
    return True, ""


def _stringify_task_field(value: object) -> str:
    if value is None:
        return "(not specified)"
    if isinstance(value, str):
        return value if value.strip() else "(not specified)"
    return json.dumps(value, indent=2)


def render_manager_plan(plan: List[Dict[str, str]]) -> str:
    rendered_tasks = []
    for task in plan:
        rendered_tasks.append(render_manager_task(task))
    return "\n\n".join(rendered_tasks)


def render_manager_task(task: Dict[str, object]) -> str:
    preferred_order = [
        "id",
        "title",
        "goal",
        "user_requirement_trace",
        "dependencies",
        "interfaces",
        "parameters",
        "control_logic",
        "datapath",
        "state_registers",
        "reset_clocking",
        "behavior",
        "edge_cases",
        "acceptance_criteria",
        "deliverable",
        "notes",
    ]
    lines = []
    for key in preferred_order:
        if key in task:
            lines.append(f"{key}: {_stringify_task_field(task.get(key))}")
    for key, value in task.items():
        if key not in preferred_order:
            lines.append(f"{key}: {_stringify_task_field(value)}")
    return "\n".join(lines)


def current_manager_handoff(state: "AgentState") -> str:
    task = current_manager_task(state)
    return (
        "Current task handoff from Manager:\n"
        f"{render_manager_task(task)}\n\n"
        "Original user requirement, authoritative source:\n"
        f"{state['user_request']}\n\n"
        "Full ordered Manager plan:\n"
        f"{render_manager_plan(state['manager_plan'])}"
    )


def validate_generated_files(
    files: object, max_file_bytes: int = 500_000, max_files: int = 64
):
    if not isinstance(files, list) or not files:
        return False, "Coding output must be a non-empty JSON list."
    if max_files and len(files) > max_files:
        return False, f"Generated file count {len(files)} exceeds limit {max_files}."
    seen_filenames = set()
    for idx, file_info in enumerate(files):
        if not isinstance(file_info, dict):
            return False, f"Item {idx} must be an object."
        if "filename" not in file_info or "content" not in file_info:
            return False, f"Item {idx} must include filename and content."
        if not isinstance(file_info["filename"], str) or not file_info["filename"].strip():
            return False, f"Item {idx} has an invalid filename."
        filename = file_info["filename"].strip()
        if filename in seen_filenames:
            return False, f"Item {idx} duplicates filename '{filename}'."
        seen_filenames.add(filename)
        if filename.startswith("."):
            return False, f"Item {idx} filename must not be hidden."
        if Path(filename).name != filename:
            return False, f"Item {idx} filename must not include path segments."
        if re.search(r"[^a-zA-Z0-9_.-]", filename):
            return False, f"Item {idx} filename contains unsupported characters."
        if Path(filename).suffix.lower() not in {".v", ".sv", ".vh", ".svh"}:
            return False, f"Item {idx} has unsupported extension."
        if not isinstance(file_info["content"], str) or not file_info["content"].strip():
            return False, f"Item {idx} has invalid content."
        content = file_info["content"]
        if len(content.encode()) > max_file_bytes:
            return False, f"Item {idx} exceeds max file size of {max_file_bytes} bytes."
        if any(marker in content for marker in ("<<<<<<<", "=======", ">>>>>>>")):
            return False, f"Item {idx} contains unresolved conflict markers."
        if Path(filename).suffix.lower() in {".v", ".sv"} and not re.search(
            r"\b(module|interface|package|primitive)\b", content
        ):
            return False, f"Item {idx} source file must contain a Verilog/SystemVerilog design unit."
    return True, ""


FILENAME_ALIASES = ("filename", "file_name", "path", "filepath", "file", "name")
CONTENT_ALIASES = (
    "content",
    "code",
    "source",
    "text",
    "body",
    "verilog",
    "systemverilog",
)
FILES_PAYLOAD_ALIASES = (
    "files",
    "rtl_files",
    "generated_files",
    "outputs",
    "artifacts",
    "testbench_files",
)


def _first_present(mapping: Dict[str, object], keys: tuple) -> object:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _unwrap_files_payload(payload: object) -> List[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in FILES_PAYLOAD_ALIASES:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if _first_present(payload, FILENAME_ALIASES) or _first_present(payload, CONTENT_ALIASES):
            return [payload]
    raise ValueError("Coding output must be a JSON list or an object containing a files list.")


def _strip_markdown_code_fence(content: str) -> str:
    text = content.strip()
    match = re.fullmatch(r"```(?:[a-zA-Z0-9_+-]+)?\s*\n(.*?)\n?```", text, flags=re.S)
    if match:
        return match.group(1).strip()
    return content


def _infer_filename_from_content(content: str, index: int) -> str:
    match = re.search(r"\b(?:module|interface|package|primitive)\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b", content)
    if match:
        return f"{match.group(1)}.sv"
    return f"generated_{index + 1}.sv"


def _normalize_filename(raw_filename: object, content: str, index: int) -> str:
    if raw_filename is None:
        filename = _infer_filename_from_content(content, index)
    else:
        filename = Path(str(raw_filename).strip()).name
        if not filename:
            filename = _infer_filename_from_content(content, index)
    filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", filename)
    filename = filename.lstrip(".") or _infer_filename_from_content(content, index)
    if Path(filename).suffix.lower() not in {".v", ".sv", ".vh", ".svh"}:
        filename += ".sv"
    return filename


def normalize_generated_files(files: object) -> List[Dict[str, str]]:
    normalized = []
    seen_filenames = set()
    for idx, file_info in enumerate(_unwrap_files_payload(files)):
        if not isinstance(file_info, dict):
            raise ValueError(f"Item {idx} must be an object.")

        raw_content = _first_present(file_info, CONTENT_ALIASES)
        if isinstance(raw_content, list):
            raw_content = "\n".join(str(line) for line in raw_content)
        if raw_content is None:
            raise ValueError(f"Item {idx} must include content/code/source.")

        content = _strip_markdown_code_fence(str(raw_content))
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        filename = _normalize_filename(_first_present(file_info, FILENAME_ALIASES), content, idx)

        unique_filename = filename
        duplicate_count = 2
        while unique_filename in seen_filenames:
            path = Path(filename)
            unique_filename = f"{path.stem}_{duplicate_count}{path.suffix}"
            duplicate_count += 1
        seen_filenames.add(unique_filename)

        normalized.append({"filename": unique_filename, "content": content})
    return normalized


def total_file_bytes(files: List[Dict[str, str]]) -> int:
    return sum(len(file_info["content"].encode()) for file_info in files)


def parse_args():
    return build_arg_parser().parse_args()


def read_user_requirement(raw_input: str) -> str:
    if raw_input.startswith("@"):
        raw_input = raw_input[1:].strip()

    possible_path = Path(raw_input).expanduser()
    try:
        if possible_path.is_file():
            return possible_path.read_text(encoding="utf-8")
    except OSError:
        return raw_input
    return raw_input


def write_text_artifact(relative_path: str, content: str):
    path = artifact_path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json_artifact(relative_path: str, content: object):
    write_text_artifact(relative_path, json.dumps(content, indent=2))


def extract_module_names(files: List[Dict[str, str]]) -> List[str]:
    names = []
    for file_info in files:
        content = re.sub(r"//.*?$|/\*.*?\*/", "", file_info["content"], flags=re.S | re.M)
        for match in re.finditer(r"\bmodule\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b", content):
            name = match.group(1)
            if name not in names:
                names.append(name)
    return names


def infer_top_module_candidates(files: List[Dict[str, str]]) -> List[str]:
    modules = extract_module_names(files)
    instantiated = set(extract_instantiated_modules(files))
    non_testbench = [
        name
        for name in modules
        if not re.search(r"(^tb_|_tb$|testbench)", name, flags=re.I)
    ]
    top_like = [name for name in non_testbench if name not in instantiated]
    return top_like or non_testbench or modules


def extract_module_name_counts(files: List[Dict[str, str]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for file_info in files:
        content = re.sub(r"//.*?$|/\*.*?\*/", "", file_info["content"], flags=re.S | re.M)
        for match in re.finditer(r"\bmodule\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b", content):
            name = match.group(1)
            counts[name] = counts.get(name, 0) + 1
    return counts


def extract_instantiated_modules(files: List[Dict[str, str]]) -> List[str]:
    primitive_or_keyword = {
        "always",
        "and",
        "assign",
        "begin",
        "case",
        "buf",
        "bufif0",
        "bufif1",
        "else",
        "end",
        "for",
        "function",
        "generate",
        "if",
        "initial",
        "module",
        "nand",
        "nor",
        "not",
        "notif0",
        "notif1",
        "or",
        "primitive",
        "pulldown",
        "pullup",
        "tran",
        "tranif0",
        "tranif1",
        "task",
        "wire",
        "xnor",
        "xor",
    }
    instances = []
    pattern = re.compile(
        r"^\s*([a-zA-Z_][a-zA-Z0-9_$]*)\s*(?:#\s*\([^;]*?\)\s*)?"
        r"([a-zA-Z_][a-zA-Z0-9_$]*)\s*\(",
        flags=re.M | re.S,
    )
    for file_info in files:
        content = re.sub(r"//.*?$|/\*.*?\*/", "", file_info["content"], flags=re.S | re.M)
        for module_name, _instance_name in pattern.findall(content):
            if module_name not in primitive_or_keyword and module_name not in instances:
                instances.append(module_name)
    return instances


def build_file_manifest(files: List[Dict[str, str]]) -> List[Dict[str, object]]:
    manifest = []
    for file_info in files:
        content = file_info["content"]
        manifest.append(
            {
                "filename": file_info["filename"],
                "bytes": len(content.encode()),
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        )
    return manifest


def hdl_sort_key(file_info: Dict[str, str]):
    filename = file_info["filename"]
    content = file_info["content"]
    suffix = Path(filename).suffix.lower()
    if suffix in {".vh", ".svh"}:
        group = 0
    elif re.search(r"\bpackage\b", content):
        group = 1
    else:
        group = 2
    return group, filename


def write_compile_order(files: List[Dict[str, str]]):
    compile_order = "\n".join(file_info["filename"] for file_info in sorted(files, key=hdl_sort_key))
    write_text_artifact("compile_order.f", compile_order + ("\n" if compile_order else ""))


def basic_rtl_sanity(
    files: List[Dict[str, str]], allow_blackboxes: bool = False
) -> Dict[str, object]:
    issues = []
    module_counts = extract_module_name_counts(files)
    defined_modules = set(module_counts)
    for module_name, count in module_counts.items():
        if count > 1:
            issues.append(f"duplicate module definition: {module_name} appears {count} times")

    for file_info in files:
        filename = file_info["filename"]
        content = file_info["content"]
        if any(marker in content for marker in ("<<<<<<<", "=======", ">>>>>>>")):
            issues.append(f"{filename}: unresolved conflict marker found")
        module_count = len(re.findall(r"\bmodule\b", content))
        endmodule_count = len(re.findall(r"\bendmodule\b", content))
        if module_count != endmodule_count:
            issues.append(
                f"{filename}: module/endmodule count mismatch ({module_count}/{endmodule_count})"
            )
        if re.search(r"\bassign\s+[^;]+(?=\n)", content):
            issues.append(f"{filename}: possible assign statement missing semicolon")

    for instance_module in extract_instantiated_modules(files):
        if instance_module not in defined_modules and not allow_blackboxes:
            issues.append(f"unresolved module instantiation: {instance_module}")

    if issues:
        return {"passed": False, "report": "\n".join(issues)}
    return {"passed": True, "report": "Basic RTL sanity PASS"}


def static_microarchitecture_review(files: List[Dict[str, str]]) -> Dict[str, object]:
    combined = "\n".join(file_info["content"] for file_info in files)
    blockers = []
    warnings = []
    sequential_blocks = len(re.findall(r"\balways_ff\b|always\s*@\s*\(", combined))
    combinational_blocks = len(re.findall(r"\balways_comb\b|always\s*@\s*\*", combined))
    control_terms = re.findall(r"\b(state|next_state|enable|valid|ready|done|error|load|clear)\b", combined)
    datapath_terms = re.findall(r"\b(count|counter|data|accum|sum|addr|ptr|fifo|mem|result)\b", combined)
    has_clocked_interface = bool(re.search(r"\b(clk|clock|reset|rst_n|rst)\b", combined))

    if has_clocked_interface and sequential_blocks == 0:
        blockers.append("clock/reset-like interface found but no sequential block is present")
    if combinational_blocks == 0 and re.search(r"\bcase\b|\bif\b", combined):
        warnings.append("no clear combinational block found for next-state/control decisions")
    if len(control_terms) < 2:
        warnings.append("few explicit control signal names found")
    if len(datapath_terms) < 2:
        warnings.append("few explicit datapath signal names found")
    if re.search(r"\balways_ff\b", combined) and not re.search(r"\balways_comb\b", combined):
        warnings.append("always_ff is used but always_comb next/control logic is not visible")

    if blockers:
        report = "\n".join(blockers + [f"warning: {warning}" for warning in warnings])
        return {"passed": False, "report": report}
    if warnings:
        return {
            "passed": True,
            "report": "Static microarchitecture review PASS with warnings:\n"
            + "\n".join(warnings),
        }
    return {"passed": True, "report": "Static microarchitecture review PASS"}


def merge_files(existing_files: List[Dict[str, str]], new_files: List[Dict[str, str]]):
    merged = {file_info["filename"]: file_info for file_info in existing_files}
    for file_info in new_files:
        merged[file_info["filename"]] = file_info
    return list(merged.values())


def render_files(files: List[Dict[str, str]]) -> str:
    if not files:
        return "(no RTL files yet)"

    rendered = []
    for file_info in files:
        rendered.append(f"--- FILE: {file_info['filename']} ---\n{file_info['content']}")
    return "\n\n".join(rendered)


def clip_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n\n[TRUNCATED: omitted {omitted} characters]"


def render_files_for_prompt(files: List[Dict[str, str]], max_chars: int) -> str:
    return clip_text(render_files(files), max_chars)


def current_manager_task(state: AgentState):
    return state["manager_plan"][state["current_task_index"]]


def run_syntax_lint(
    files: List[Dict[str, str]], require_tool: bool = False, timeout_seconds: int = 30
) -> Dict[str, object]:
    if not files:
        return {"passed": False, "report": "No files were available for lint."}

    lint_tool = discover_lint_tool()
    if not lint_tool:
        if require_tool:
            return {
                "passed": False,
                "report": "Syntax lint failed: --require-lint was set but neither verilator nor iverilog is installed.",
            }
        return {
            "passed": True,
            "report": "Syntax lint skipped: neither verilator nor iverilog is installed.",
        }

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        file_paths = []
        for file_info in files:
            filename = Path(file_info["filename"]).name
            file_path = tmp_path / filename
            file_path.write_text(file_info["content"])
            file_paths.append(file_path)

        if Path(lint_tool).name == "verilator":
            cmd = [lint_tool, "--lint-only", "--timing"] + [str(path) for path in file_paths]
        else:
            cmd = [lint_tool, "-tnull", "-g2012"] + [str(path) for path in file_paths]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return {
                "passed": False,
                "report": f"{Path(lint_tool).name} timed out after {timeout_seconds} seconds.",
            }
        output = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode == 0:
            return {"passed": True, "report": f"{Path(lint_tool).name} PASS"}
        return {
            "passed": False,
            "report": f"{Path(lint_tool).name} FAIL\n{output}",
        }


# --- 4. Define Agents ---
from verilog_agent import runtime as agent_runtime
from verilog_agent.architecture_team import architecture_agent, architecture_review_agent
from verilog_agent.coding_team import microarchitecture_reviewer_agent, verilog_coding_team_agent
from verilog_agent.control_datapath_team import (
    control_datapath_planner_agent,
    control_datapath_review_agent,
)
from verilog_agent.intake_manager import intake_agent, manager_agent
from verilog_agent.review_writer import final_review_agent, summary_agent, writer_agent
from verilog_agent.supervisor_team import (
    supervisor_accept_agent,
    supervisor_agent,
    supervisor_review_agent,
)
from verilog_agent.testbench_team import final_lint_agent, testbench_team_agent
from verilog_agent.verification_team import verification_team_agent

agent_runtime.bind(sys.modules[__name__])

writer_tool_node = ToolNode(writer_tools)


# --- 5. Conditional Edges ---
def coding_condition(state: AgentState):
    if state.get("generation_ok"):
        return "microarch_review"
    if state.get("coding_retry_count", 0) >= state.get("max_retries", 3):
        print("---CONDITION: Max coding retries reached. Failing run before final review.---")
        return "fail"
    return "retry"


def architecture_review_condition(state: AgentState):
    if state.get("architecture_review_passed"):
        return "supervisor"
    if state.get("architecture_retry_count", 0) >= state.get("max_architecture_retries", 2):
        print("---CONDITION: Max architecture review retries reached. Continuing with latest contract.---")
        return "supervisor"
    return "retry"


def supervisor_review_condition(state: AgentState):
    if state.get("supervisor_review_passed"):
        return "control_datapath"
    if state.get("supervisor_retry_count", 0) >= state.get("max_supervisor_retries", 2):
        print("---CONDITION: Max supervisor review retries reached. Continuing with latest task packet.---")
        return "control_datapath"
    return "retry"


def control_datapath_review_condition(state: AgentState):
    if state.get("control_datapath_review_passed"):
        return "coding"
    if state.get("control_datapath_retry_count", 0) >= state.get("max_control_datapath_retries", 2):
        print("---CONDITION: Max Control/Data Path review retries reached. Continuing with latest plan.---")
        return "coding"
    return "retry"


def microarchitecture_condition(state: AgentState):
    if state.get("microarchitecture_passed"):
        return "verify"
    if state.get("microarchitecture_retry_count", 0) >= state.get("max_retries", 3):
        print("---CONDITION: Max microarchitecture retries reached. Failing run before final review.---")
        return "fail"
    return "retry"


def verification_condition(state: AgentState):
    if state.get("verification_passed"):
        return "accept"
    if state.get("verification_retry_count", 0) >= state.get("max_retries", 3):
        print("---CONDITION: Max verification retries reached. Failing run before final review.---")
        return "fail"
    return "retry"


def next_task_condition(state: AgentState):
    if state["current_task_index"] >= len(state["manager_plan"]):
        if state.get("skip_testbench"):
            return "final_lint"
        return "testbench"
    return "next_task"


def testbench_condition(state: AgentState):
    if state.get("generation_ok"):
        return "final_lint"
    if state.get("testbench_retry_count", 0) >= state.get("max_testbench_retries", 2):
        print("---CONDITION: Max testbench retries reached. Failing run before final lint.---")
        return "fail"
    return "retry"


def final_lint_condition(state: AgentState):
    if state.get("final_lint_passed"):
        return "review"
    print("---CONDITION: Final lint failed. Failing run before writer.---")
    return "fail"


def final_review_condition(state: AgentState):
    if state.get("human_approved"):
        return "write"
    return "summary"



# --- 6. Build the LangGraph Workflow ---
workflow = StateGraph(AgentState)

workflow.add_node("intake", intake_agent)
workflow.add_node("manager", manager_agent)
workflow.add_node("architecture", architecture_agent)
workflow.add_node("architecture_review", architecture_review_agent)
workflow.add_node("supervisor", supervisor_agent)
workflow.add_node("supervisor_review", supervisor_review_agent)
workflow.add_node("control_datapath_planner", control_datapath_planner_agent)
workflow.add_node("control_datapath_review", control_datapath_review_agent)
workflow.add_node("verilog_coding_team", verilog_coding_team_agent)
workflow.add_node("microarchitecture_reviewer", microarchitecture_reviewer_agent)
workflow.add_node("verification_team", verification_team_agent)
workflow.add_node("supervisor_accept", supervisor_accept_agent)
workflow.add_node("testbench_team", testbench_team_agent)
workflow.add_node("final_lint", final_lint_agent)
workflow.add_node("final_review", final_review_agent)
workflow.add_node("summary", summary_agent)
workflow.add_node("writer", writer_agent)
workflow.add_node("writer_tool", writer_tool_node)

workflow.set_entry_point("intake")

workflow.add_edge("intake", "manager")
workflow.add_conditional_edges(
    "manager",
    lambda state: "fail" if state.get("failed_stage") == "manager" else "architecture",
    {"architecture": "architecture", "fail": "summary"},
)
workflow.add_edge("architecture", "architecture_review")
workflow.add_conditional_edges(
    "architecture_review",
    architecture_review_condition,
    {"supervisor": "supervisor", "retry": "architecture"},
)
workflow.add_edge("supervisor", "supervisor_review")
workflow.add_conditional_edges(
    "supervisor_review",
    supervisor_review_condition,
    {"control_datapath": "control_datapath_planner", "retry": "supervisor"},
)
workflow.add_edge("control_datapath_planner", "control_datapath_review")
workflow.add_conditional_edges(
    "control_datapath_review",
    control_datapath_review_condition,
    {"coding": "verilog_coding_team", "retry": "control_datapath_planner"},
)
workflow.add_conditional_edges(
    "verilog_coding_team",
    coding_condition,
    {
        "microarch_review": "microarchitecture_reviewer",
        "retry": "verilog_coding_team",
        "fail": "summary",
    },
)
workflow.add_conditional_edges(
    "microarchitecture_reviewer",
    microarchitecture_condition,
    {"verify": "verification_team", "retry": "verilog_coding_team", "fail": "summary"},
)
workflow.add_conditional_edges(
    "verification_team",
    verification_condition,
    {"accept": "supervisor_accept", "retry": "verilog_coding_team", "fail": "summary"},
)
workflow.add_conditional_edges(
    "supervisor_accept",
    next_task_condition,
    {"next_task": "supervisor", "testbench": "testbench_team", "final_lint": "final_lint"},
)
workflow.add_conditional_edges(
    "testbench_team",
    testbench_condition,
    {"final_lint": "final_lint", "retry": "testbench_team", "fail": "summary"},
)
workflow.add_conditional_edges(
    "final_lint",
    final_lint_condition,
    {"review": "final_review", "fail": "summary"},
)
workflow.add_conditional_edges(
    "final_review",
    final_review_condition,
    {"write": "writer", "summary": "summary"},
)
workflow.add_conditional_edges(
    "writer",
    lambda state: "tool" if state.get("final_files") or state.get("testbench_files") else "end",
    {"tool": "writer_tool", "end": END},
)
workflow.add_edge("writer_tool", "summary")
workflow.add_edge("summary", END)

app = workflow.compile()


# --- 7. Run the Application ---
if __name__ == "__main__":
    args = parse_args()
    user_request_input = args.spec or ""
    if not user_request_input.strip():
        user_request_input = input(
            "Describe the RTL you want to build, or enter a spec file path / @path (or 'exit'): "
        ).strip()
    if user_request_input.lower() == "exit":
        sys.exit("Exiting.")
    initial_user_request = read_user_requirement(user_request_input)

    run_timestamp = datetime.now()
    ARTIFACT_DIR = resolve_artifact_dir(args, initial_user_request, run_timestamp)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    llm = create_llm(
        args.llm_provider,
        args.llm_model,
        args.llm_temperature,
        args.llm_api_url,
        args.llm_api_key,
    )
    active_llm_config = llm_config(
        args.llm_provider,
        args.llm_model,
        args.llm_temperature,
        args.llm_api_url,
        args.llm_api_key,
    )
    if args.auto_approve:
        os.environ["AUTO_APPROVE_FINAL"] = "true"

    execution_config = {
        "run_id": run_id,
        "argv": sys.argv[1:],
        "artifact_dir": str(ARTIFACT_DIR),
        "artifact_dir_auto_named": not bool(args.artifact_dir or os.getenv("ARTIFACT_DIR")),
        "artifact_dir_timestamp": run_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "project_keyword": derive_project_keyword(initial_user_request),
        "auto_approve": args.auto_approve,
        "skip_testbench": args.no_testbench,
        "require_lint": args.require_lint,
        "lint_tool": discover_lint_tool(),
        "lint_timeout_seconds": args.lint_timeout,
        "allow_blackboxes": args.allow_blackboxes,
        "max_generated_file_bytes": args.max_generated_file_bytes,
        "max_generated_files": args.max_generated_files,
        "max_context_chars": args.max_context_chars,
        "max_user_request_chars": args.max_user_request_chars,
        "max_manager_tasks": args.max_manager_tasks,
        "fail_on_manager_fallback": args.fail_on_manager_fallback,
        "retry_limits": {
            "architecture": args.max_architecture_retries,
            "supervisor": args.max_supervisor_retries,
            "control_datapath": args.max_control_datapath_retries,
            "coding": args.max_retries,
            "microarchitecture": args.max_retries,
            "verification": args.max_retries,
            "testbench": args.max_testbench_retries,
        },
        "llm_config": active_llm_config,
    }
    write_json_artifact("execution_config.json", execution_config)

    print("Verilog Coding Agent (Manager / Supervisor / Coding / Verification)")
    print(
        f"LLM: provider={active_llm_config['provider']} "
        f"backend={active_llm_config['backend']} model={active_llm_config['model']}"
    )
    initial_state = {
        "messages": [],
        "run_id": run_id,
        "user_request": initial_user_request,
        "manager_plan": [],
        "architecture_contract": "",
        "architecture_review_passed": False,
        "architecture_review_report": "",
        "architecture_retry_count": 0,
        "max_architecture_retries": args.max_architecture_retries,
        "current_task_index": 0,
        "supervisor_plan": "",
        "supervisor_review_passed": False,
        "supervisor_review_report": "",
        "supervisor_retry_count": 0,
        "max_supervisor_retries": args.max_supervisor_retries,
        "control_datapath_plan": "",
        "control_datapath_review_passed": False,
        "control_datapath_review_report": "",
        "control_datapath_retry_count": 0,
        "max_control_datapath_retries": args.max_control_datapath_retries,
        "microarchitecture_passed": False,
        "microarchitecture_report": "",
        "coding_retry_count": 0,
        "microarchitecture_retry_count": 0,
        "verification_retry_count": 0,
        "testbench_retry_count": 0,
        "max_testbench_retries": args.max_testbench_retries,
        "rtl_context": "",
        "candidate_files": [],
        "final_files": [],
        "testbench_files": [],
        "top_module_candidates": [],
        "verification_passed": False,
        "verification_report": "",
        "lint_report": "",
        "require_lint": args.require_lint,
        "lint_timeout_seconds": args.lint_timeout,
        "allow_blackboxes": args.allow_blackboxes,
        "final_lint_passed": False,
        "final_lint_report": "",
        "human_approved": False,
        "skip_testbench": args.no_testbench,
        "max_retries": args.max_retries,
        "run_status": "",
        "failed_stage": "",
        "blocking_report": "",
        "max_generated_file_bytes": args.max_generated_file_bytes,
        "max_generated_files": args.max_generated_files,
        "max_context_chars": args.max_context_chars,
        "max_user_request_chars": args.max_user_request_chars,
        "max_manager_tasks": args.max_manager_tasks,
        "manager_fallback_used": False,
        "fail_on_manager_fallback": args.fail_on_manager_fallback,
        "generation_ok": False,
        "error_message": "",
    }
    try:
        result = app.invoke(initial_state)
        print("\nProcess finished.")
        print("Generated files:")
        for message in result.get("messages", []):
            if isinstance(message, ToolMessage):
                print(f"- {message.content}")
    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Exiting.")
        sys.exit(0)
