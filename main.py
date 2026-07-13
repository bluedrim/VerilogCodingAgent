import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Dict, List, Optional, TypedDict

from verilog_agent.llm_config import add_llm_arguments


DEFAULT_MAX_RETRIES = 3


def positive_int(raw_value: str) -> int:
    value = int(raw_value)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be 0 or greater")
    return value


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Verilog coding agent.")
    parser.add_argument("--spec", help="RTL requirement text or path to a requirement file.")
    parser.add_argument(
        "--max-retries",
        type=positive_int,
        default=os.getenv("MAX_RETRIES", str(DEFAULT_MAX_RETRIES)),
        help=(
            "Force-forward threshold for coding local review gates, "
            "microarchitecture review, and verification. Defaults to MAX_RETRIES or 3; "
            "set 0 to disable force-forward."
        ),
    )
    parser.add_argument(
        "--max-architecture-retries",
        type=positive_int,
        default=os.getenv(
            "MAX_ARCHITECTURE_RETRIES",
            os.getenv("MAX_RETRIES", str(DEFAULT_MAX_RETRIES)),
        ),
        help=(
            "Architecture review force-forward threshold. At this count the run proceeds "
            "to Supervisor with the best available architecture. Defaults to "
            "MAX_ARCHITECTURE_RETRIES or MAX_RETRIES; set 0 to disable force-forward."
        ),
    )
    parser.add_argument(
        "--max-supervisor-retries",
        type=positive_int,
        default=os.getenv(
            "MAX_SUPERVISOR_RETRIES",
            os.getenv("MAX_RETRIES", str(DEFAULT_MAX_RETRIES)),
        ),
        help=(
            "Supervisor review force-forward threshold. At this count the run proceeds "
            "to Control/Data Path instead of failing. Defaults to MAX_SUPERVISOR_RETRIES "
            "or MAX_RETRIES; set 0 to disable force-forward."
        ),
    )
    parser.add_argument(
        "--max-control-datapath-retries",
        type=positive_int,
        default=os.getenv(
            "MAX_CONTROL_DATAPATH_RETRIES",
            os.getenv("MAX_RETRIES", str(DEFAULT_MAX_RETRIES)),
        ),
        help=(
            "Control/Data Path review force-forward threshold. At this count the run proceeds "
            "to Coding with the best available plan. Defaults to "
            "MAX_CONTROL_DATAPATH_RETRIES or MAX_RETRIES; set 0 to disable force-forward."
        ),
    )
    parser.add_argument(
        "--max-testbench-retries",
        type=positive_int,
        default=os.getenv(
            "MAX_TESTBENCH_RETRIES",
            os.getenv("MAX_RETRIES", str(DEFAULT_MAX_RETRIES)),
        ),
        help=(
            "Final lint force-forward threshold after testbench repair attempts. "
            "At this count the run proceeds to final review. Defaults to "
            "MAX_TESTBENCH_RETRIES or MAX_RETRIES; set 0 to disable force-forward."
        ),
    )
    parser.add_argument(
        "--graph-recursion-limit",
        type=positive_int,
        default=int(os.getenv("GRAPH_RECURSION_LIMIT", "10000")),
        help=(
            "LangGraph safety recursion limit. This is not a stage retry limit; "
            "increase it for very long repair runs."
        ),
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
        "--continue",
        dest="continue_run",
        action="store_true",
        help=(
            "Continue an interrupted or failed run from its latest checkpoint. "
            "Requires --artifact-dir (or ARTIFACT_DIR) pointing to an existing output directory."
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
    add_llm_arguments(parser)
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
    from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

    warnings.filterwarnings(
        "ignore",
        message=r"The default value of `allowed_objects` will change in a future version.*",
        category=LangChainPendingDeprecationWarning,
    )

    from dotenv import load_dotenv
    from langchain_core.messages import (
        AIMessage,
        BaseMessage,
        HumanMessage,
        ToolMessage,
        message_to_dict,
        messages_from_dict,
    )
    from langchain_core.prompts import ChatPromptTemplate
    from langgraph.graph import END, StateGraph

    from tools import write_verilog_file
    from verilog_agent.llm_config import (
        create_llm,
        llm_config,
        normalize_chat_completions_url,
        public_llm_config,
        resolve_llm_settings,
    )
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
    architecture_review_forced_forward: bool
    architecture_retry_count: int
    max_architecture_retries: int
    current_task_index: int
    supervisor_plan: str
    supervisor_review_passed: bool
    supervisor_review_report: str
    supervisor_review_forced_forward: bool
    supervisor_retry_count: int
    max_supervisor_retries: int
    control_datapath_plan: str
    control_datapath_review_passed: bool
    control_datapath_review_report: str
    control_datapath_review_forced_forward: bool
    control_datapath_retry_count: int
    max_control_datapath_retries: int
    microarchitecture_passed: bool
    microarchitecture_review_forced_forward: bool
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
    verification_review_forced_forward: bool
    verification_report: str
    lint_report: str
    require_lint: bool
    lint_timeout_seconds: int
    llm_timeout_seconds: int
    allow_blackboxes: bool
    final_lint_passed: bool
    final_lint_forced_forward: bool
    final_lint_report: str
    writer_results: List[str]
    writer_errors: List[str]
    human_approved: bool
    skip_testbench: bool
    max_retries: int
    run_status: str
    failed_stage: str
    blocking_report: str
    review_feedback_log: List[Dict[str, str]]
    max_generated_file_bytes: int
    max_generated_files: int
    max_context_chars: int
    max_user_request_chars: int
    max_manager_tasks: int
    manager_fallback_used: bool
    fail_on_manager_fallback: bool
    run_id: str
    generation_ok: bool
    coding_review_forced_forward: bool
    error_message: str
    resume_stage: str


# --- 3. Setup Runtime and Tools ---
def discover_lint_tool() -> str:
    lint_tool = shutil.which("verilator") or shutil.which("iverilog")
    return lint_tool or ""


llm = None
active_llm_config = llm_config()
ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "generated_rtl"))
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


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


def load_prompt(filename: str) -> str:
    prompt_path = PROMPT_DIR / filename
    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}") from exc


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
    "pass_with_warnings",
    "yes",
    "ok",
    "success",
    "successful",
    "valid",
    "approve",
    "approved",
    "accepted",
    "통과",
    "합격",
    "승인",
    "정상",
    "성공",
    "문제없음",
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
    "changes_requested",
    "reject",
    "rejected",
    "denied",
    "deny",
    "실패",
    "불합격",
    "거절",
    "오류",
    "에러",
    "무효",
    "수정필요",
    "변경필요",
}
BOOLEAN_FALSE_VALUES = {"false", "no", "0", "none", "null", "n/a"}


def parse_review_result(raw_content: str, invalid_json_report: str):
    passed, report, _details = parse_review_result_with_details(raw_content, invalid_json_report)
    return passed, report


def parse_review_result_with_details(raw_content: str, invalid_json_report: str):
    details = {
        "source": "",
        "json_parsed": False,
        "fallback_text_verdict": None,
        "nonblocking_report_match": False,
        "blocking_report_match": False,
        "error": "",
    }
    try:
        result = _load_json(raw_content)
        details["json_parsed"] = True
        details["source"] = "json"
        passed, report = _parse_review_json_result(result)
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
        details["source"] = "text"
        details["error"] = str(exc)
        fallback_passed = _parse_review_text_verdict(raw_content)
        details["fallback_text_verdict"] = fallback_passed
        report = _compact_review_text(raw_content) or invalid_json_report
        if fallback_passed is None:
            passed = False
            report = invalid_json_report
        else:
            passed = fallback_passed
    details["passed"] = passed
    details["report"] = report
    details["nonblocking_report_match"] = _review_report_is_nonblocking_pass(report)
    details["blocking_report_match"] = _review_report_has_blocking_failure(report)
    return passed, report, details


def _parse_review_json_result(result: object):
    if isinstance(result, list) and len(result) == 1:
        result = result[0]
    if not isinstance(result, dict):
        raise TypeError("Review JSON result must be an object.")

    result = {_normalize_review_key(key): value for key, value in result.items()}
    passed = _review_pass_value(result)
    report = _review_report_value(result)
    if not passed and _review_report_is_nonblocking_pass(report):
        passed = True
    if passed and _review_report_has_blocking_failure(report):
        passed = False
    return passed, report


def _review_pass_value(result: Dict[str, object]) -> bool:
    pass_keys = (
        "pass",
        "passed",
        "is_passed",
        "review_passed",
        "passed_review",
        "ok",
        "success",
        "valid",
        "approved",
        "accepted",
    )
    for key in pass_keys:
        if key in result:
            return _json_bool(result.get(key))

    failure_keys = (
        "fail",
        "failed",
        "has_failures",
        "has_errors",
        "error",
        "invalid",
        "rejected",
        "needs_changes",
        "changes_requested",
        "request_changes",
    )
    for key in failure_keys:
        if key in result:
            return _negated_failure_value(result.get(key))

    for key in ("result", "status", "verdict", "decision", "outcome", "review", "review_result"):
        if key not in result:
            continue
        value = _normalize_review_token(result.get(key))
        if value in TRUE_REVIEW_VALUES:
            return True
        if value in FALSE_REVIEW_VALUES:
            return False
    return False


def _review_report_value(result: Dict[str, object]) -> str:
    for key in (
        "report",
        "reason",
        "feedback",
        "summary",
        "message",
        "details",
        "findings",
        "comments",
        "notes",
    ):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return "\n".join(str(item).strip() for item in value if str(item).strip())
    return json.dumps(result, ensure_ascii=False)


def _review_report_is_nonblocking_pass(report: str) -> bool:
    normalized = _normalize_review_text(report)
    if not normalized:
        return False
    negative_context_patterns = (
        r"\bnot\s+(?:complete\s+enough|implementation[-\s]?ready|sufficient|valid)\b",
        r"\binsufficient\s+for\s+(?:rtl\s+)?(?:coding|implementation)\b",
    )
    if any(re.search(pattern, normalized) for pattern in negative_context_patterns):
        return False
    nonblocking_patterns = (
        r"\bpass(?:ed)?\s+with\s+warnings?\b",
        r"\bwarnings?\s+only\b",
        r"\bno\s+blocking\s+(?:issues?|gaps?|problems?|findings?|failures?)\b",
        r"\bno\s+blockers?\b",
        r"\bno\s+required\s+(?:fixes?|changes?)\b",
        r"\bnon[-\s]?blocking\s+(?:only|suggestions?|warnings?)\b",
        r"\bcomplete\s+enough\b",
        r"\bimplementation[-\s]?ready\b",
        r"\bsufficient\s+for\s+(?:rtl\s+)?(?:coding|implementation)\b",
        r"차단\s*(?:이슈|문제|항목|결함)\s*없",
        r"블로커\s*없",
        r"경고만",
        r"비차단",
    )
    return any(re.search(pattern, normalized) for pattern in nonblocking_patterns)


def _review_report_has_blocking_failure(report: str) -> bool:
    normalized = _normalize_review_text(report)
    if not normalized:
        return False
    if _review_report_is_nonblocking_pass(normalized):
        return False
    blocking_patterns = (
        r"\bblocking\s+(?:issues?|gaps?|problems?|findings?|failures?)\b",
        r"\bblockers?\s*:",
        r"\bmust\s+(?:fix|change|add|define|specify|resolve)\b",
        r"\brequired\s+(?:interface|reset|clock|datapath|control|fsm|state|width|module|signal)[^.\n;:]*\bmissing\b",
        r"\bmissing\s+required\s+(?:interface|reset|clock|datapath|control|fsm|state|width|module|signal)\b",
        r"\bnot\s+(?:complete|implementation[-\s]?ready|sufficient|valid)\b",
        r"\bdoes\s+not\s+(?:satisfy|meet|match|compile|pass)\b",
        r"\bsyntax\s+(?:error|fail|failed)\b",
        r"\b(?:verilog|rtl)\s+(?:error|invalid)\b",
        r"차단\s*(?:이슈|문제|항목|결함)",
        r"블로커\s*:",
        r"필수.*누락",
        r"수정\s*필요",
        r"컴파일\s*(?:실패|오류|에러)",
    )
    return any(re.search(pattern, normalized) for pattern in blocking_patterns)


def _parse_review_text_verdict(raw_content: str) -> Optional[bool]:
    lowered = _normalize_review_text(raw_content)
    if _review_report_is_nonblocking_pass(lowered):
        return True
    if _review_report_has_blocking_failure(lowered):
        return False
    if re.search(r"\b(no|without)\s+(failures?|errors?|issues?|blocking\s+issues?)\b", lowered):
        return True
    if re.search(r"(문제\s*없|오류\s*없|에러\s*없|차단\s*(?:이슈|문제)\s*없|통과|합격|승인)", lowered):
        return True
    if re.search(r"\b(fail|failed|reject|rejected|invalid|not\s+pass|does\s+not\s+pass)\b", lowered):
        return False
    if re.search(r"(실패|불합격|거절|무효|수정\s*필요|변경\s*필요)", lowered):
        return False
    if re.search(r"\b(pass|passed|approve|approved|ok|valid|success|looks\s+good)\b", lowered):
        return True
    return None


def _compact_review_text(raw_content: str, limit: int = 2000) -> str:
    compact = raw_content.strip()
    if not compact:
        return ""
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "\n... [truncated]"


def _normalize_review_text(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _json_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return _normalize_review_token(value) in TRUE_REVIEW_VALUES
    return False


def _negated_failure_value(value: object) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return not bool(value)
    if isinstance(value, str):
        stripped = _normalize_review_token(value)
        if stripped in BOOLEAN_FALSE_VALUES:
            return True
        if stripped in TRUE_REVIEW_VALUES or stripped in FALSE_REVIEW_VALUES:
            return False
    return False


def _normalize_review_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _normalize_review_token(value: object) -> str:
    token = str(value or "").strip().lower()
    token = re.sub(r"^[^\w]+|[^\w]+$", "", token)
    token = re.sub(r"[^\w]+", "_", token)
    if token.startswith("pass_"):
        return "pass"
    if token.startswith("approved_") or token.startswith("approve_"):
        return "approved"
    return token


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


def parse_manager_plan_response(raw_content: str, max_tasks: int = 32) -> List[Dict[str, object]]:
    plan = _load_json(raw_content)
    plan = _normalize_manager_plan_shape(plan)
    is_valid, validation_error = validate_plan(plan, max_tasks)
    if not is_valid:
        raise ValueError(validation_error)
    return plan


def _normalize_manager_plan_shape(plan: object) -> List[Dict[str, object]]:
    if isinstance(plan, dict):
        for key in ("tasks", "plan", "manager_plan", "implementation_tasks"):
            value = plan.get(key)
            if isinstance(value, list):
                plan = value
                break
    if not isinstance(plan, list):
        raise ValueError("Manager plan must be a JSON list or an object containing a task list.")

    normalized = []
    for idx, item in enumerate(plan, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Task {idx - 1} must be an object.")
        task = dict(item)
        task.setdefault("id", f"T{idx}")
        task.setdefault("title", f"Task {idx}")
        if "goal" not in task:
            task["goal"] = task.get("description") or task.get("objective") or task.get("summary") or ""
        if "deliverable" not in task:
            task["deliverable"] = task.get("output") or task.get("result") or ""
        for key, value in list(task.items()):
            if key in {"id", "title", "goal", "deliverable"}:
                task[key] = "TBD" if value is None else str(value)
            elif value is None:
                task[key] = "TBD"
        normalized.append(task)
    return normalized


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


def append_review_feedback(
    state: "AgentState", stage: str, report: str, task_id: str | None = None
) -> List[Dict[str, str]]:
    feedback_log = list(state.get("review_feedback_log", []))
    clean_report = str(report or "").strip()
    if not clean_report:
        clean_report = "Review failed without a detailed report."
    if task_id is None:
        try:
            task_id = str(current_manager_task(state).get("id") or "global")
        except (IndexError, KeyError, TypeError):
            task_id = "global"
    feedback_log.append(
        {
            "stage": str(stage),
            "task_id": str(task_id),
            "report": clean_report,
        }
    )
    return feedback_log[-20:]


def render_review_feedback(
    state: "AgentState", stages: tuple[str, ...] | None = None, max_chars: int = 12000
) -> str:
    entries = state.get("review_feedback_log", [])
    if stages:
        stage_set = set(stages)
        entries = [entry for entry in entries if entry.get("stage") in stage_set]
    if not entries:
        return "(none)"
    rendered = []
    for idx, entry in enumerate(entries, start=1):
        rendered.append(
            f"[{idx}] stage={entry.get('stage', 'unknown')} "
            f"task={entry.get('task_id', 'global')}\n{entry.get('report', '').strip()}"
        )
    return clip_text("\n\n".join(rendered), max_chars)


CODING_REPAIR_BACKLOG_STAGES = (
    "supervisor_review",
    "control_datapath_review",
    "microarchitecture_review",
    "verification",
    "verification_lint",
    "coding_format",
    "coding_gate_internal",
    "coding_unchanged",
    "coding_preflight",
)


CODING_BACKLOG_SNAPSHOT_STAGES = {
    "microarchitecture_review",
    "verification",
    "verification_lint",
}


def active_coding_feedback_entries(state: "AgentState") -> List[Dict[str, str]]:
    entries = review_feedback_entries(state, CODING_REPAIR_BACKLOG_STAGES)
    latest_snapshot_index = -1
    for idx, entry in enumerate(entries):
        if entry.get("stage") in CODING_BACKLOG_SNAPSHOT_STAGES:
            latest_snapshot_index = idx
    if latest_snapshot_index >= 0:
        entries = entries[latest_snapshot_index:]
    return entries


def render_coding_repair_backlog(state: "AgentState", max_chars: int = 12000) -> str:
    entries = active_coding_feedback_entries(state)
    if not entries:
        return "(none)"

    deduped = []
    seen = set()
    for entry in entries:
        report = str(entry.get("report", "")).strip()
        key = (
            str(entry.get("stage", "unknown")),
            str(entry.get("task_id", "global")),
            normalized_text_fingerprint(report)[:16],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)

    lines = [
        "Cumulative coding repair backlog:",
        "- These are previous unresolved coding repair items unless the current RTL clearly fixed them.",
        "- Reviewers must keep still-unresolved previous items and add new findings in the next FAIL packet.",
        "- Coding Team must close this backlog with a broader coordinated RTL edit, not isolated tiny tweaks.",
        "",
    ]
    for idx, entry in enumerate(deduped[-12:], start=1):
        report = str(entry.get("report", "")).strip() or "Review failed without details."
        lines.append(
            f"{idx}. prior_stage={entry.get('stage', 'unknown')} "
            f"task={entry.get('task_id', 'global')}"
        )
        lines.append(f"   unresolved_or_verify_fixed: {report}")
    return clip_text("\n".join(lines), max_chars)


def review_feedback_entries(
    state: "AgentState", stages: tuple[str, ...] | None = None
) -> List[Dict[str, str]]:
    entries = state.get("review_feedback_log", [])
    if stages:
        stage_set = set(stages)
        entries = [entry for entry in entries if entry.get("stage") in stage_set]
    return list(entries)


def render_revision_mode(
    state: "AgentState",
    stages: tuple[str, ...] | None,
    artifact_name: str,
    retry_count_key: str,
) -> str:
    attempt = state.get(retry_count_key, 0) + 1
    if review_feedback_entries(state, stages):
        return (
            f"review-driven revision attempt {attempt}; update the previous {artifact_name} "
            "so every reviewer finding is addressed before returning the next result"
        )
    return f"fresh {artifact_name} generation attempt {attempt}"


def render_revision_checklist(
    state: "AgentState",
    stages: tuple[str, ...] | None,
    artifact_name: str,
    max_chars: int = 12000,
) -> str:
    entries = review_feedback_entries(state, stages)
    if not entries:
        return "(none)"
    lines = [
        f"Review-driven {artifact_name} revision checklist:",
        "- Treat each item below as a blocking fix unless it explicitly says informational.",
        f"- Revise the previous {artifact_name}; do not return the same content unchanged.",
        "- Preserve already-correct decisions and interfaces unless the reviewer requires a change.",
        "- The next reviewer should not be able to repeat the same finding.",
        "",
    ]
    for idx, entry in enumerate(entries[-8:], start=1):
        report = str(entry.get("report", "")).strip() or "Review failed without details."
        lines.append(
            f"{idx}. stage={entry.get('stage', 'unknown')} task={entry.get('task_id', 'global')}"
        )
        lines.append(f"   required_fix: {report}")
    return clip_text("\n".join(lines), max_chars)


def normalized_text_fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", "", str(text or "")).lower()
    return hashlib.sha256(normalized.encode()).hexdigest()


def unchanged_review_revision_report(
    state: "AgentState",
    stages: tuple[str, ...] | None,
    artifact_name: str,
    previous_text: str,
    new_text: str,
) -> str:
    if not review_feedback_entries(state, stages):
        return ""
    if not str(previous_text or "").strip():
        return ""
    if normalized_text_fingerprint(previous_text) != normalized_text_fingerprint(new_text):
        return ""
    return (
        f"{artifact_name} was returned unchanged after reviewer feedback. "
        f"The team must revise the previous {artifact_name} so the review findings are directly addressed."
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
        if Path(filename).suffix.lower() not in {".v", ".vh"}:
            return False, f"Item {idx} must use Verilog-only .v or .vh extension."
        if not isinstance(file_info["content"], str) or not file_info["content"].strip():
            return False, f"Item {idx} has invalid content."
        content = file_info["content"]
        if len(content.encode()) > max_file_bytes:
            return False, f"Item {idx} exceeds max file size of {max_file_bytes} bytes."
        if any(marker in content for marker in ("<<<<<<<", "=======", ">>>>>>>")):
            return False, f"Item {idx} contains unresolved conflict markers."
        stripped_content = strip_hdl_comments(content)
        sv_error = find_systemverilog_construct(stripped_content)
        if sv_error:
            return False, f"Item {idx} uses SystemVerilog construct: {sv_error}."
        if Path(filename).suffix.lower() == ".v" and not re.search(
            r"\b(module|primitive)\b", stripped_content
        ):
            return False, f"Item {idx} source file must contain a Verilog module or primitive."
    return True, ""


def strip_hdl_comments(content: str) -> str:
    return re.sub(r"//.*?$|/\*.*?\*/", "", content, flags=re.S | re.M)


def find_systemverilog_construct(content: str) -> str:
    patterns = [
        (r"\balways_ff\b", "always_ff"),
        (r"\balways_comb\b", "always_comb"),
        (r"\balways_latch\b", "always_latch"),
        (r"\blogic\b", "logic"),
        (r"\bbit\b", "bit"),
        (r"\bbyte\b", "byte"),
        (r"\bshortint\b", "shortint"),
        (r"\bint\b", "int"),
        (r"\blongint\b", "longint"),
        (r"\btypedef\b", "typedef"),
        (r"\benum\b", "enum"),
        (r"\bstruct\b", "struct"),
        (r"\bunion\b", "union"),
        (r"\binterface\b", "interface"),
        (r"\bendinterface\b", "endinterface"),
        (r"\bmodport\b", "modport"),
        (r"\bpackage\b", "package"),
        (r"\bendpackage\b", "endpackage"),
        (r"\bimport\b", "import"),
        (r"\bclass\b", "class"),
        (r"\bendclass\b", "endclass"),
        (r"\bunique\b", "unique"),
        (r"\bpriority\b", "priority"),
        (r"\bfinal\b", "final"),
        (r"\bassert\b", "assert"),
        (r"\bcovergroup\b", "covergroup"),
        (r"\bproperty\b", "property"),
    ]
    for pattern, name in patterns:
        if re.search(pattern, content):
            return name
    return ""


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
    match = re.search(
        r"^\s*(?:module|primitive)\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b",
        content,
        flags=re.M,
    )
    if match:
        return f"{match.group(1)}.v"
    return f"generated_{index + 1}.v"


def _normalize_filename(raw_filename: object, content: str, index: int) -> str:
    if raw_filename is None:
        filename = _infer_filename_from_content(content, index)
    else:
        filename = Path(str(raw_filename).strip()).name
        if not filename:
            filename = _infer_filename_from_content(content, index)
    filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", filename)
    filename = filename.lstrip(".") or _infer_filename_from_content(content, index)
    if Path(filename).suffix.lower() not in {".v", ".vh"}:
        filename = f"{Path(filename).stem}.v"
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


def _extract_filename_hint(text: str, index: int) -> str:
    patterns = (
        r"(?:filename|file_name|file|path)\s*[:=]\s*[`'\"]?([a-zA-Z0-9_.-]+\.(?:v|vh|sv|svh))",
        r"^\s*#+\s*`?([a-zA-Z0-9_.-]+\.(?:v|vh|sv|svh))`?\s*$",
        r"^\s*//\s*([a-zA-Z0-9_.-]+\.(?:v|vh|sv|svh))\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.M)
        if match:
            return _normalize_filename(match.group(1), "", index)
    return ""


def _verilog_module_blocks(content: str) -> List[str]:
    blocks = []
    strict_pattern = re.compile(
        r"^\s*(?:module|primitive)\s+[a-zA-Z_][a-zA-Z0-9_$]*\b.*?"
        r"^\s*end(?:module|primitive)\b",
        flags=re.S | re.M,
    )
    for match in strict_pattern.finditer(content):
        blocks.append(match.group(0).strip())
    if blocks:
        return blocks

    loose_pattern = re.compile(
        r"\b(?:module|primitive)\s+[a-zA-Z_][a-zA-Z0-9_$]*\b.*?"
        r"\bend(?:module|primitive)\b",
        flags=re.S,
    )
    for match in loose_pattern.finditer(content):
        blocks.append(match.group(0).strip().strip('"`'))
    return blocks


def _append_recovered_file(
    recovered: List[Dict[str, str]], filename: str, content: str, index: int
) -> None:
    content = content.strip()
    if not content:
        return
    stripped = strip_hdl_comments(content)
    if not re.search(r"\b(?:module|primitive)\b", stripped):
        return
    if not re.search(r"\bend(?:module|primitive)\b", stripped):
        return
    recovered.append(
        {
            "filename": filename or _infer_filename_from_content(content, index),
            "content": content,
        }
    )


def recover_verilog_files_from_text(raw_content: str) -> List[Dict[str, str]]:
    recovered = []
    fence_pattern = re.compile(r"```([^\n`]*)\n(.*?)\n?```", flags=re.S | re.I)
    for idx, match in enumerate(fence_pattern.finditer(raw_content)):
        fence_info = match.group(1).strip()
        content = match.group(2).strip()
        if not re.search(r"^\s*(?:module|primitive)\b", content, flags=re.M):
            continue
        prefix = raw_content[max(0, match.start() - 300) : match.start()]
        filename = _extract_filename_hint(f"{prefix}\n{fence_info}", idx)
        blocks = _verilog_module_blocks(content)
        if len(blocks) > 1 and not filename:
            for block in blocks:
                _append_recovered_file(recovered, "", block, len(recovered))
        else:
            _append_recovered_file(
                recovered,
                filename or _infer_filename_from_content(content, idx),
                content,
                idx,
            )

    if recovered:
        return normalize_generated_files(recovered)

    content = raw_content.strip()
    blocks = _verilog_module_blocks(content)
    for block in blocks:
        _append_recovered_file(recovered, "", block, len(recovered))
    if recovered:
        return normalize_generated_files(recovered)

    if re.search(r"^\s*(?:module|primitive)\b", content, flags=re.M) and re.search(
        r"^\s*end(?:module|primitive)\b", content, flags=re.M
    ):
        _append_recovered_file(
            recovered,
            _infer_filename_from_content(content, 0),
            content,
            0,
        )
        if recovered:
            return normalize_generated_files(recovered)
    raise ValueError("No recoverable Verilog module was found outside JSON.")


def parse_generated_files_response(raw_content: str) -> List[Dict[str, str]]:
    try:
        files = _load_json(raw_content)
        return normalize_generated_files(files)
    except (json.JSONDecodeError, TypeError, ValueError) as json_exc:
        try:
            return recover_verilog_files_from_text(raw_content)
        except ValueError as recover_exc:
            raise ValueError(f"{json_exc}; fallback recovery failed: {recover_exc}") from json_exc


def render_file_blocks(files: List[Dict[str, str]]) -> str:
    blocks = []
    for file_info in files:
        blocks.append(
            f"FILE: {file_info['filename']}\n"
            "```verilog\n"
            f"{file_info['content'].rstrip()}\n"
            "```"
        )
    return "\n\n".join(blocks)


def validate_coding_candidate_files(
    files: object,
    max_file_bytes: int = 500_000,
    max_files: int = 64,
):
    is_valid, validation_error = validate_generated_files(files, max_file_bytes, max_files)
    if not is_valid:
        return False, validation_error
    sanity_result = basic_rtl_sanity(files, allow_blackboxes=True)
    if not sanity_result["passed"]:
        return False, f"Basic Verilog sanity failed:\n{sanity_result['report']}"
    return True, ""


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
    _write_dashboard_heartbeat(relative_path, path)


def _write_dashboard_heartbeat(relative_path: str, written_path: Path):
    if str(relative_path) == "dashboard_heartbeat.json":
        return
    try:
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "artifact_dir": str(ARTIFACT_DIR),
            "process_id": os.getpid(),
            "last_artifact": str(relative_path),
            "last_artifact_path": str(written_path),
            "last_artifact_bytes": written_path.stat().st_size,
        }
        heartbeat = ARTIFACT_DIR / "dashboard_heartbeat.json"
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def write_json_artifact(relative_path: str, content: object):
    write_text_artifact(relative_path, json.dumps(content, indent=2))


CHECKPOINT_FILENAME = "run_state_checkpoint.json"
CHECKPOINT_VERSION = 1


def _checkpoint_state_to_json(state: Dict[str, object]) -> Dict[str, object]:
    serialized = dict(state)
    serialized_messages = []
    for message in state.get("messages", []):
        try:
            serialized_messages.append(message_to_dict(message))
        except (TypeError, ValueError):
            serialized_messages.append(
                {
                    "type": "ai",
                    "data": {"content": str(getattr(message, "content", message))},
                }
            )
    serialized["messages"] = serialized_messages
    return serialized


def _checkpoint_state_from_json(raw_state: object) -> Dict[str, object]:
    if not isinstance(raw_state, dict):
        raise ValueError("Checkpoint state must be a JSON object.")
    restored = dict(raw_state)
    raw_messages = restored.get("messages", [])
    if not isinstance(raw_messages, list):
        raise ValueError("Checkpoint messages must be a JSON array.")
    try:
        restored["messages"] = messages_from_dict(raw_messages)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Checkpoint messages are invalid: {exc}") from exc
    return restored


def _merge_state_update(
    state: Dict[str, object], update: Dict[str, object] | None
) -> Dict[str, object]:
    merged = dict(state)
    if not update:
        return merged
    for key, value in update.items():
        if key == "messages":
            merged[key] = list(state.get(key, [])) + list(value or [])
        else:
            merged[key] = value
    return merged


def write_run_checkpoint(
    state: Dict[str, object],
    resume_stage: str,
    *,
    phase: str,
    last_completed_node: str = "",
    error: str = "",
) -> None:
    checkpoint_path = artifact_path(CHECKPOINT_FILENAME)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_state = dict(state)
    checkpoint_state["resume_stage"] = resume_stage
    payload = {
        "version": CHECKPOINT_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "resume_stage": resume_stage,
        "last_completed_node": last_completed_node,
        "error": error,
        "state": _checkpoint_state_to_json(checkpoint_state),
    }
    temporary_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary_path, checkpoint_path)
    _write_dashboard_heartbeat(CHECKPOINT_FILENAME, checkpoint_path)


def read_run_checkpoint(artifact_dir: Path) -> tuple[Dict[str, object], Dict[str, object]]:
    checkpoint_path = artifact_dir / CHECKPOINT_FILENAME
    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"No {CHECKPOINT_FILENAME} exists in {artifact_dir}. "
            "Only runs created after checkpoint support was added can be continued."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read continuation checkpoint: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Continuation checkpoint must be a JSON object.")
    if payload.get("version") != CHECKPOINT_VERSION:
        raise ValueError(
            f"Unsupported checkpoint version: {payload.get('version')!r}; "
            f"expected {CHECKPOINT_VERSION}."
        )
    return _checkpoint_state_from_json(payload.get("state")), payload


DEFAULT_CODE_PROMPT_KEYS = {
    "accepted_rtl_files",
    "candidate_rtl",
    "current_candidate_rtl",
    "invalid_output",
    "previous_candidate_rtl",
    "previous_testbench_files",
    "rtl_context",
}


def _render_prompt_template(template: str, payload: Dict[str, str]) -> str:
    try:
        return template.format(**payload)
    except Exception as exc:
        keys = ", ".join(sorted(payload))
        return (
            template
            + "\n\n[Prompt render preview failed: "
            + f"{exc}. Available payload keys: {keys}]"
        )


def log_agent_prompt(
    agent_name: str,
    attempt: object,
    system_prompt: str,
    human_template: str,
    payload: Dict[str, object],
    code_keys: tuple[str, ...] | None = None,
) -> str:
    """Write a reviewable snapshot of the exact agent input message."""
    safe_agent = sanitize_artifact_name(agent_name, "agent")
    safe_attempt = sanitize_artifact_name(attempt, "attempt")
    prefix = f"logs/agent_messages/{safe_agent}_attempt_{safe_attempt}"
    rendered_payload = {key: str(value) for key, value in payload.items()}
    externalized_keys = set(DEFAULT_CODE_PROMPT_KEYS)
    if code_keys:
        externalized_keys.update(code_keys)

    snapshot_payload = {}
    externalized = {}
    for key, value in rendered_payload.items():
        if key in externalized_keys and value.strip() and value.strip() != "(none)":
            key_name = sanitize_artifact_name(key, "payload")
            artifact = f"{prefix}_{key_name}.md"
            write_text_artifact(artifact, value)
            snapshot_payload[key] = f"[externalized payload: {artifact}; chars={len(value)}]"
            externalized[key] = artifact
        else:
            snapshot_payload[key] = value

    human_message = _render_prompt_template(human_template, snapshot_payload)
    sizes = {key: len(value) for key, value in rendered_payload.items()}
    try:
        max_chars = int(os.getenv("AGENT_MESSAGE_LOG_MAX_CHARS", "300000"))
    except ValueError:
        max_chars = 300000
    content = [
        "# Agent Message Snapshot",
        "",
        f"- agent: {safe_agent}",
        f"- attempt: {safe_attempt}",
        f"- system_chars: {len(system_prompt)}",
        f"- rendered_human_chars: {len(human_message)}",
        "",
        "## Payload Sizes",
        "",
        "```json",
        json.dumps(sizes, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Externalized Code/RTL Payloads",
        "",
    ]
    if externalized:
        content.extend(f"- {key}: {path}" for key, path in sorted(externalized.items()))
    else:
        content.append("(none)")
    content.extend(
        [
            "",
            "## System Message",
            "",
            system_prompt,
            "",
            "## Human Message",
            "",
            clip_text(human_message, max_chars),
        ]
    )
    path = f"{prefix}.md"
    write_text_artifact(path, "\n".join(content))
    return path


def extract_module_names(files: List[Dict[str, str]]) -> List[str]:
    names = []
    for file_info in files:
        content = strip_hdl_comments(file_info["content"])
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
        content = strip_hdl_comments(file_info["content"])
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
        content = strip_hdl_comments(file_info["content"])
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


def generated_files_fingerprint(files: List[Dict[str, str]]) -> str:
    """Stable fingerprint for deciding whether a reviewed RTL candidate changed."""
    entries = []
    for file_info in sorted(files, key=lambda item: item.get("filename", "")):
        filename = str(file_info.get("filename", "")).strip()
        content = str(file_info.get("content", ""))
        entries.append(
            {
                "filename": filename,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        )
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def hdl_sort_key(file_info: Dict[str, str]):
    filename = file_info["filename"]
    content = file_info["content"]
    suffix = Path(filename).suffix.lower()
    if suffix == ".vh":
        group = 0
    else:
        group = 1
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
        stripped_content = strip_hdl_comments(content)
        if any(marker in content for marker in ("<<<<<<<", "=======", ">>>>>>>")):
            issues.append(f"{filename}: unresolved conflict marker found")
        module_count = len(re.findall(r"\bmodule\b", stripped_content))
        endmodule_count = len(re.findall(r"\bendmodule\b", stripped_content))
        if module_count != endmodule_count:
            issues.append(
                f"{filename}: module/endmodule count mismatch ({module_count}/{endmodule_count})"
            )
        if re.search(r"\bassign\s+[^;]+(?=\n)", stripped_content):
            issues.append(f"{filename}: possible assign statement missing semicolon")

    for instance_module in extract_instantiated_modules(files):
        if instance_module not in defined_modules and not allow_blackboxes:
            issues.append(f"unresolved module instantiation: {instance_module}")

    if issues:
        return {"passed": False, "report": "\n".join(issues)}
    return {"passed": True, "report": "Basic RTL sanity PASS"}


def static_microarchitecture_review(files: List[Dict[str, str]]) -> Dict[str, object]:
    combined = "\n".join(strip_hdl_comments(file_info["content"]) for file_info in files)
    blockers = []
    warnings = []
    sv_error = find_systemverilog_construct(combined)
    if sv_error:
        blockers.append(f"SystemVerilog construct is not allowed: {sv_error}")
    sequential_blocks = len(re.findall(r"always\s*@\s*\(", combined))
    combinational_blocks = len(re.findall(r"always\s*@\s*\*", combined))
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


def split_context_budget(max_chars: int, sections: int = 8) -> int:
    if max_chars <= 0:
        return 0
    sections = max(sections, 1)
    return max(max_chars // sections, 1)


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
            cmd = [lint_tool, "-tnull", "-g2005"] + [str(path) for path in file_paths]

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

# --- 5. Conditional Edges ---
def coding_condition(state: AgentState):
    if state.get("generation_ok"):
        return "microarch_review"
    if state.get("coding_review_forced_forward"):
        print("---CONDITION: Coding local review force-forward threshold reached. Continuing to Microarchitecture review with best available RTL candidate.---")
        return "microarch_review"
    print("---CONDITION: Coding attempt needs repair. Retrying.---")
    return "retry"


def architecture_review_condition(state: AgentState):
    if state.get("architecture_review_passed") or state.get("architecture_review_forced_forward"):
        return "supervisor"
    print("---CONDITION: Architecture review failed. Retrying.---")
    return "retry"


def architecture_generation_condition(state: AgentState):
    if state.get("failed_stage") != "architecture_generation":
        return "review"
    print("---CONDITION: Architecture generation preflight failed. Retrying Architecture Team before review without a stage retry cap.---")
    return "retry"


def supervisor_review_condition(state: AgentState):
    if state.get("supervisor_review_passed"):
        return "control_datapath"
    force_forward_after = state.get("max_supervisor_retries", DEFAULT_MAX_RETRIES)
    if force_forward_after and state.get("supervisor_retry_count", 0) >= force_forward_after:
        print("---CONDITION: Supervisor review force-forward threshold reached. Continuing to Control/Data Path planning with best available packet.---")
        return "control_datapath"
    print("---CONDITION: Supervisor review failed. Retrying until force-forward threshold.---")
    return "retry"


def supervisor_generation_condition(state: AgentState):
    if state.get("failed_stage") != "supervisor_generation":
        return "review"
    print("---CONDITION: Supervisor generation preflight failed. Retrying Supervisor before review without a stage retry cap.---")
    return "retry"


def control_datapath_review_condition(state: AgentState):
    if state.get("control_datapath_review_passed") or state.get("control_datapath_review_forced_forward"):
        return "coding"
    print("---CONDITION: Control/Data Path review failed. Retrying.---")
    return "retry"


def control_datapath_generation_condition(state: AgentState):
    if state.get("failed_stage") != "control_datapath_generation":
        return "review"
    print("---CONDITION: Control/Data Path generation preflight failed. Retrying planner before review without a stage retry cap.---")
    return "retry"


def microarchitecture_condition(state: AgentState):
    if state.get("microarchitecture_passed") or state.get("microarchitecture_review_forced_forward"):
        return "verify"
    print("---CONDITION: Microarchitecture review failed. Returning to Coding Team.---")
    return "retry"


def verification_condition(state: AgentState):
    if state.get("verification_passed") or state.get("verification_review_forced_forward"):
        return "accept"
    print("---CONDITION: Verification failed. Returning to Coding Team.---")
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
    print("---CONDITION: Testbench generation needs repair. Retrying without a stage retry cap.---")
    return "retry"


def final_lint_condition(state: AgentState):
    if state.get("final_lint_passed") or state.get("final_lint_forced_forward"):
        return "review"
    if state.get("skip_testbench"):
        print("---CONDITION: Final lint failed with testbench disabled. Failing run before writer.---")
        return "fail"
    print("---CONDITION: Final lint failed. Returning to Testbench Team with lint feedback until force-forward threshold.---")
    return "retry"


def final_review_condition(state: AgentState):
    if state.get("human_approved"):
        return "write"
    return "summary"


CHECKPOINT_STAGES = {
    "intake",
    "manager",
    "architecture",
    "architecture_review",
    "supervisor",
    "supervisor_review",
    "control_datapath_planner",
    "control_datapath_review",
    "verilog_coding_team",
    "microarchitecture_reviewer",
    "verification_team",
    "supervisor_accept",
    "testbench_team",
    "final_lint",
    "final_review",
    "writer",
    "summary",
}


def _failed_stage_resume_target(state: AgentState) -> str:
    failed_stage = str(state.get("failed_stage") or "").lower()
    if failed_stage == "manager":
        return "manager"
    if failed_stage.startswith("architecture"):
        return "architecture"
    if failed_stage.startswith("supervisor"):
        return "supervisor"
    if failed_stage.startswith("control_datapath"):
        return "control_datapath_planner"
    if failed_stage.startswith(("coding", "microarchitecture", "verification")):
        return "verilog_coding_team"
    if failed_stage.startswith(("testbench", "final_lint")):
        return "testbench_team"
    if failed_stage == "final_review":
        return "final_review"
    if failed_stage == "writer":
        return "writer"
    return ""


def _resume_target_after_summary(state: AgentState) -> str:
    if (
        state.get("run_status") == "passed"
        and state.get("human_approved")
        and not state.get("writer_errors")
    ):
        return ""
    failed_target = _failed_stage_resume_target(state)
    if failed_target:
        return failed_target
    manager_plan = state.get("manager_plan", [])
    current_task_index = state.get("current_task_index", 0)
    if isinstance(manager_plan, list) and isinstance(current_task_index, int):
        if current_task_index < len(manager_plan):
            return "supervisor"
    if state.get("writer_errors"):
        return "writer"
    if state.get("final_files") and not state.get("human_approved"):
        return "final_review"
    return ""


def checkpoint_resume_target(completed_node: str, state: AgentState) -> str:
    if completed_node == "intake":
        return "manager"
    if completed_node == "manager":
        return "summary" if state.get("failed_stage") == "manager" else "architecture"
    if completed_node == "architecture":
        if state.get("failed_stage") != "architecture_generation":
            return "architecture_review"
        return "architecture"
    if completed_node == "architecture_review":
        if state.get("architecture_review_passed") or state.get(
            "architecture_review_forced_forward"
        ):
            return "supervisor"
        return "architecture"
    if completed_node == "supervisor":
        if state.get("failed_stage") != "supervisor_generation":
            return "supervisor_review"
        return "supervisor"
    if completed_node == "supervisor_review":
        if state.get("supervisor_review_passed"):
            return "control_datapath_planner"
        retry_limit = state.get("max_supervisor_retries", DEFAULT_MAX_RETRIES)
        if retry_limit and state.get("supervisor_retry_count", 0) >= retry_limit:
            return "control_datapath_planner"
        return "supervisor"
    if completed_node == "control_datapath_planner":
        if state.get("failed_stage") != "control_datapath_generation":
            return "control_datapath_review"
        return "control_datapath_planner"
    if completed_node == "control_datapath_review":
        if state.get("control_datapath_review_passed") or state.get(
            "control_datapath_review_forced_forward"
        ):
            return "verilog_coding_team"
        return "control_datapath_planner"
    if completed_node == "verilog_coding_team":
        if state.get("generation_ok") or state.get("coding_review_forced_forward"):
            return "microarchitecture_reviewer"
        return "verilog_coding_team"
    if completed_node == "microarchitecture_reviewer":
        if state.get("microarchitecture_passed") or state.get(
            "microarchitecture_review_forced_forward"
        ):
            return "verification_team"
        return "verilog_coding_team"
    if completed_node == "verification_team":
        if state.get("verification_passed") or state.get(
            "verification_review_forced_forward"
        ):
            return "supervisor_accept"
        return "verilog_coding_team"
    if completed_node == "supervisor_accept":
        if state.get("current_task_index", 0) < len(state.get("manager_plan", [])):
            return "supervisor"
        return "final_lint" if state.get("skip_testbench") else "testbench_team"
    if completed_node == "testbench_team":
        return "final_lint" if state.get("generation_ok") else "testbench_team"
    if completed_node == "final_lint":
        if state.get("final_lint_passed") or state.get("final_lint_forced_forward"):
            return "final_review"
        return "summary" if state.get("skip_testbench") else "testbench_team"
    if completed_node == "final_review":
        return "writer" if state.get("human_approved") else "summary"
    if completed_node == "writer":
        return "summary"
    if completed_node == "summary":
        return _resume_target_after_summary(state)
    raise ValueError(f"Unknown checkpoint node: {completed_node}")


def checkpointed_agent(node_name: str, agent):
    def wrapped(state: AgentState):
        write_run_checkpoint(state, node_name, phase="before", last_completed_node="")
        try:
            update = agent(state) or {}
        except BaseException as exc:
            write_run_checkpoint(
                state,
                node_name,
                phase="interrupted",
                error=f"{type(exc).__name__}: {exc}"[:4000],
            )
            raise
        merged_state = _merge_state_update(state, update)
        next_stage = checkpoint_resume_target(node_name, merged_state)
        write_run_checkpoint(
            merged_state,
            next_stage,
            phase="after",
            last_completed_node=node_name,
        )
        return update

    wrapped.__name__ = f"checkpointed_{node_name}"
    return wrapped


def resume_dispatch_agent(state: AgentState):
    return {}


def resume_dispatch_condition(state: AgentState) -> str:
    stage = str(state.get("resume_stage") or "intake")
    if stage not in CHECKPOINT_STAGES:
        raise ValueError(f"Checkpoint has unsupported resume stage: {stage!r}")
    return stage



# --- 6. Build the LangGraph Workflow ---
workflow = StateGraph(AgentState)

workflow.add_node("resume_dispatch", resume_dispatch_agent)
workflow.add_node("intake", checkpointed_agent("intake", intake_agent))
workflow.add_node("manager", checkpointed_agent("manager", manager_agent))
workflow.add_node("architecture", checkpointed_agent("architecture", architecture_agent))
workflow.add_node(
    "architecture_review",
    checkpointed_agent("architecture_review", architecture_review_agent),
)
workflow.add_node("supervisor", checkpointed_agent("supervisor", supervisor_agent))
workflow.add_node(
    "supervisor_review", checkpointed_agent("supervisor_review", supervisor_review_agent)
)
workflow.add_node(
    "control_datapath_planner",
    checkpointed_agent("control_datapath_planner", control_datapath_planner_agent),
)
workflow.add_node(
    "control_datapath_review",
    checkpointed_agent("control_datapath_review", control_datapath_review_agent),
)
workflow.add_node(
    "verilog_coding_team",
    checkpointed_agent("verilog_coding_team", verilog_coding_team_agent),
)
workflow.add_node(
    "microarchitecture_reviewer",
    checkpointed_agent("microarchitecture_reviewer", microarchitecture_reviewer_agent),
)
workflow.add_node(
    "verification_team", checkpointed_agent("verification_team", verification_team_agent)
)
workflow.add_node(
    "supervisor_accept", checkpointed_agent("supervisor_accept", supervisor_accept_agent)
)
workflow.add_node("testbench_team", checkpointed_agent("testbench_team", testbench_team_agent))
workflow.add_node("final_lint", checkpointed_agent("final_lint", final_lint_agent))
workflow.add_node("final_review", checkpointed_agent("final_review", final_review_agent))
workflow.add_node("summary", checkpointed_agent("summary", summary_agent))
workflow.add_node("writer", checkpointed_agent("writer", writer_agent))

workflow.set_entry_point("resume_dispatch")
workflow.add_conditional_edges(
    "resume_dispatch",
    resume_dispatch_condition,
    {stage: stage for stage in CHECKPOINT_STAGES},
)

workflow.add_edge("intake", "manager")
workflow.add_conditional_edges(
    "manager",
    lambda state: "fail" if state.get("failed_stage") == "manager" else "architecture",
    {"architecture": "architecture", "fail": "summary"},
)
workflow.add_conditional_edges(
    "architecture",
    architecture_generation_condition,
    {"review": "architecture_review", "retry": "architecture", "fail": "summary"},
)
workflow.add_conditional_edges(
    "architecture_review",
    architecture_review_condition,
    {"supervisor": "supervisor", "retry": "architecture", "fail": "summary"},
)
workflow.add_conditional_edges(
    "supervisor",
    supervisor_generation_condition,
    {"review": "supervisor_review", "retry": "supervisor", "fail": "summary"},
)
workflow.add_conditional_edges(
    "supervisor_review",
    supervisor_review_condition,
    {"control_datapath": "control_datapath_planner", "retry": "supervisor", "fail": "summary"},
)
workflow.add_conditional_edges(
    "control_datapath_planner",
    control_datapath_generation_condition,
    {"review": "control_datapath_review", "retry": "control_datapath_planner", "fail": "summary"},
)
workflow.add_conditional_edges(
    "control_datapath_review",
    control_datapath_review_condition,
    {"coding": "verilog_coding_team", "retry": "control_datapath_planner", "fail": "summary"},
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
    {"review": "final_review", "retry": "testbench_team", "fail": "summary"},
)
workflow.add_conditional_edges(
    "final_review",
    final_review_condition,
    {"write": "writer", "summary": "summary"},
)
workflow.add_edge("writer", "summary")
workflow.add_edge("summary", END)

app = workflow.compile()


# --- 7. Run the Application ---
if __name__ == "__main__":
    args = parse_args()
    run_timestamp = datetime.now()
    resumed_state = None
    resume_checkpoint = None
    if args.continue_run:
        configured_dir = args.artifact_dir or os.getenv("ARTIFACT_DIR")
        if not configured_dir:
            raise SystemExit("--continue requires --artifact-dir or ARTIFACT_DIR.")
        ARTIFACT_DIR = Path(configured_dir).expanduser()
        if not ARTIFACT_DIR.is_dir():
            raise SystemExit(f"Continuation artifact directory does not exist: {ARTIFACT_DIR}")
        resumed_state, resume_checkpoint = read_run_checkpoint(ARTIFACT_DIR)
        initial_user_request = str(resumed_state.get("user_request") or "").strip()
        if not initial_user_request:
            raise SystemExit("Continuation checkpoint does not contain the original requirement.")
        resume_stage = str(resume_checkpoint.get("resume_stage") or "")
        if not resume_stage:
            raise SystemExit("This run is complete and has no pending stage to continue.")
        if resume_stage not in CHECKPOINT_STAGES:
            raise SystemExit(f"Checkpoint has unsupported resume stage: {resume_stage!r}")
    else:
        user_request_input = args.spec or ""
        if not user_request_input.strip():
            user_request_input = input(
                "Describe the RTL you want to build, or enter a spec file path / @path (or 'exit'): "
            ).strip()
        if user_request_input.lower() == "exit":
            sys.exit("Exiting.")
        initial_user_request = read_user_requirement(user_request_input)
        ARTIFACT_DIR = resolve_artifact_dir(args, initial_user_request, run_timestamp)
    os.environ["ARTIFACT_DIR"] = str(ARTIFACT_DIR)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    llm = create_llm(
        args.llm_provider,
        args.llm_model,
        args.llm_temperature,
        args.llm_api_url,
        args.llm_api_key,
        args.llm_timeout,
    )
    active_llm_config = llm_config(
        args.llm_provider,
        args.llm_model,
        args.llm_temperature,
        args.llm_api_url,
        args.llm_api_key,
        args.llm_timeout,
    )
    if args.auto_approve:
        os.environ["AUTO_APPROVE_FINAL"] = "true"

    previous_execution_config = {}
    if args.continue_run:
        try:
            previous_execution_config = json.loads(
                (ARTIFACT_DIR / "execution_config.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            previous_execution_config = {}
    continuation_history = list(previous_execution_config.get("continuation_history", []))
    if args.continue_run:
        continuation_history.append(
            {
                "run_id": run_id,
                "continued_at": datetime.now(timezone.utc).isoformat(),
                "resume_stage": str(resume_checkpoint.get("resume_stage") or ""),
                "checkpoint_saved_at": str(resume_checkpoint.get("saved_at") or ""),
            }
        )
    execution_config = {
        "run_id": run_id,
        "argv": sys.argv[1:],
        "artifact_dir": str(ARTIFACT_DIR),
        "artifact_dir_auto_named": not bool(args.artifact_dir or os.getenv("ARTIFACT_DIR")),
        "artifact_dir_timestamp": run_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "project_keyword": derive_project_keyword(initial_user_request),
        "continued": args.continue_run,
        "resume_stage": str(resume_checkpoint.get("resume_stage") or "")
        if resume_checkpoint
        else "intake",
        "original_run_id": previous_execution_config.get("original_run_id")
        or previous_execution_config.get("run_id")
        or run_id,
        "continuation_history": continuation_history,
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
        "stage_retry_limits_enforced": True,
        "review_force_forward_after": {
            "architecture": args.max_architecture_retries,
            "supervisor": args.max_supervisor_retries,
            "control_datapath": args.max_control_datapath_retries,
            "coding_local_gate": args.max_retries,
            "microarchitecture": args.max_retries,
            "verification": args.max_retries,
            "final_lint": args.max_testbench_retries,
        },
        "llm_config": active_llm_config,
        "llm_timeout_seconds": args.llm_timeout
        if args.llm_timeout is not None
        else int(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
        "graph_recursion_limit": args.graph_recursion_limit,
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
        "architecture_review_forced_forward": False,
        "architecture_retry_count": 0,
        "max_architecture_retries": args.max_architecture_retries,
        "current_task_index": 0,
        "supervisor_plan": "",
        "supervisor_review_passed": False,
        "supervisor_review_report": "",
        "supervisor_review_forced_forward": False,
        "supervisor_retry_count": 0,
        "max_supervisor_retries": args.max_supervisor_retries,
        "control_datapath_plan": "",
        "control_datapath_review_passed": False,
        "control_datapath_review_report": "",
        "control_datapath_review_forced_forward": False,
        "control_datapath_retry_count": 0,
        "max_control_datapath_retries": args.max_control_datapath_retries,
        "microarchitecture_passed": False,
        "microarchitecture_review_forced_forward": False,
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
        "verification_review_forced_forward": False,
        "verification_report": "",
        "lint_report": "",
        "require_lint": args.require_lint,
        "lint_timeout_seconds": args.lint_timeout,
        "allow_blackboxes": args.allow_blackboxes,
        "llm_timeout_seconds": args.llm_timeout
        if args.llm_timeout is not None
        else int(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
        "final_lint_passed": False,
        "final_lint_forced_forward": False,
        "final_lint_report": "",
        "writer_results": [],
        "writer_errors": [],
        "human_approved": False,
        "skip_testbench": args.no_testbench,
        "max_retries": args.max_retries,
        "run_status": "",
        "failed_stage": "",
        "blocking_report": "",
        "review_feedback_log": [],
        "max_generated_file_bytes": args.max_generated_file_bytes,
        "max_generated_files": args.max_generated_files,
        "max_context_chars": args.max_context_chars,
        "max_user_request_chars": args.max_user_request_chars,
        "max_manager_tasks": args.max_manager_tasks,
        "manager_fallback_used": False,
        "fail_on_manager_fallback": args.fail_on_manager_fallback,
        "generation_ok": False,
        "coding_review_forced_forward": False,
        "error_message": "",
        "resume_stage": "intake",
    }
    if resumed_state is not None:
        initial_state.update(resumed_state)
        initial_state.update(
            {
                "run_id": run_id,
                "user_request": initial_user_request,
                "resume_stage": str(resume_checkpoint.get("resume_stage") or ""),
                "run_status": "running",
                "max_architecture_retries": args.max_architecture_retries,
                "max_supervisor_retries": args.max_supervisor_retries,
                "max_control_datapath_retries": args.max_control_datapath_retries,
                "max_testbench_retries": args.max_testbench_retries,
                "max_retries": args.max_retries,
                "require_lint": args.require_lint,
                "lint_timeout_seconds": args.lint_timeout,
                "allow_blackboxes": args.allow_blackboxes,
                "llm_timeout_seconds": args.llm_timeout
                if args.llm_timeout is not None
                else int(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
                "skip_testbench": args.no_testbench,
                "max_generated_file_bytes": args.max_generated_file_bytes,
                "max_generated_files": args.max_generated_files,
                "max_context_chars": args.max_context_chars,
                "max_user_request_chars": args.max_user_request_chars,
                "max_manager_tasks": args.max_manager_tasks,
                "fail_on_manager_fallback": args.fail_on_manager_fallback,
            }
        )
        print(
            f"Continuing {ARTIFACT_DIR} from stage "
            f"{initial_state['resume_stage']} (checkpoint {resume_checkpoint.get('saved_at', 'unknown')})."
        )
    try:
        result = app.invoke(initial_state, config={"recursion_limit": args.graph_recursion_limit})
        print("\nProcess finished.")
        print("Generated files:")
        writer_results = result.get("writer_results", [])
        if writer_results:
            for item in writer_results:
                print(f"- {item}")
        else:
            for file_info in result.get("final_files", []) + result.get("testbench_files", []):
                print(f"- {ARTIFACT_DIR / file_info.get('filename', '')}")
    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Exiting.")
        sys.exit(0)
