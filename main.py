import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Dict, List, TypedDict


def positive_int(raw_value: str) -> int:
    value = int(raw_value)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be 0 or greater")
    return value


if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
    help_parser = argparse.ArgumentParser(description="Run the Verilog coding agent.")
    help_parser.add_argument("--spec", help="RTL requirement text or path to a requirement file.")
    help_parser.add_argument(
        "--max-retries",
        type=positive_int,
        default=3,
        help="Maximum retries per coding, microarchitecture review, and verification stage.",
    )
    help_parser.add_argument(
        "--max-architecture-retries",
        type=positive_int,
        default=2,
        help="Maximum architecture review retries.",
    )
    help_parser.add_argument(
        "--max-supervisor-retries",
        type=positive_int,
        default=2,
        help="Maximum Supervisor review retries.",
    )
    help_parser.add_argument(
        "--max-control-datapath-retries",
        type=positive_int,
        default=2,
        help="Maximum Control/Data Path plan review retries.",
    )
    help_parser.add_argument(
        "--max-testbench-retries",
        type=positive_int,
        default=2,
        help="Maximum smoke testbench generation retries.",
    )
    help_parser.add_argument(
        "--artifact-dir",
        default=os.getenv("ARTIFACT_DIR", "generated_rtl"),
        help="Directory for generated artifacts and logs.",
    )
    help_parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip final interactive approval and write generated files.",
    )
    help_parser.add_argument(
        "--no-testbench",
        action="store_true",
        help="Skip smoke testbench generation.",
    )
    help_parser.add_argument(
        "--require-lint",
        action="store_true",
        help="Fail when neither verilator nor iverilog is installed.",
    )
    help_parser.add_argument(
        "--llm-provider",
        choices=["ollama", "gpt-oss"],
        help="LLM provider/backend. 'gpt-oss' supports Ollama or OpenAI-compatible endpoints.",
    )
    help_parser.add_argument("--llm-model", help="LLM model name, for example gpt-oss:20b.")
    help_parser.add_argument("--llm-temperature", type=float, help="LLM temperature.")
    help_parser.add_argument(
        "--llm-api-url",
        help="OpenAI-compatible chat completions URL, for example http://abc.net:30001/chat/completions.",
    )
    help_parser.add_argument("--llm-api-key", help="API key for an OpenAI-compatible gpt-oss endpoint.")
    help_parser.parse_args()
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
    final_lint_passed: bool
    final_lint_report: str
    human_approved: bool
    skip_testbench: bool
    max_retries: int
    run_status: str
    failed_stage: str
    blocking_report: str
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
    resolved_temperature = (
        temperature if temperature is not None else float(os.getenv("LLM_TEMPERATURE", "0.1"))
    )
    resolved_api_url = api_url or os.getenv("GPT_OSS_API_URL") or os.getenv("LLM_API_URL") or ""
    resolved_api_key = api_key or os.getenv("GPT_OSS_API_KEY") or os.getenv("LLM_API_KEY") or ""

    if resolved_provider == "gpt-oss":
        resolved_model = model or os.getenv("GPT_OSS_MODEL") or os.getenv("LLM_MODEL") or "gpt-oss"
        backend = "openai-compatible" if resolved_api_url else "ollama"
    else:
        resolved_model = model or os.getenv("LLM_MODEL") or os.getenv("OLLAMA_MODEL") or "gpt-oss:20b"
        backend = resolved_provider

    if backend == "openai-compatible":
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

    if backend == "openai-compatible":
        try:
            from langchain_openai import ChatOpenAI
        except ModuleNotFoundError as exc:
            print(f"Missing dependency: {exc.name}")
            print("Install project dependencies with: python3 -m pip install -r requirements.txt")
            sys.exit(1)
        return ChatOpenAI(
            model=model_name,
            temperature=resolved_temperature,
            base_url=str(settings["base_url"]),
            api_key=str(settings["api_key"] or "dummy"),
        )

    if backend != "ollama":
        raise ValueError("Unsupported LLM backend. Supported backends: ollama, openai-compatible")

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


llm = create_llm()
active_llm_config = llm_config()
writer_tools = [write_verilog_file]
ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "generated_rtl"))


def _strip_json_fence(raw_content: str) -> str:
    content = raw_content.strip()
    if content.startswith("```json"):
        content = content[7:].strip()
    elif content.startswith("```"):
        content = content[3:].strip()
    if content.endswith("```"):
        content = content[:-3].strip()
    return content


def _extract_json_candidate(raw_content: str) -> str:
    content = _strip_json_fence(raw_content)
    if content.startswith("[") or content.startswith("{"):
        return content

    for opener, closer in (("[", "]"), ("{", "}")):
        start = content.find(opener)
        if start == -1:
            continue

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
                    return content[start : idx + 1]

    return content


def _load_json(raw_content: str):
    return json.loads(_extract_json_candidate(raw_content))


def _json_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "pass", "passed", "yes"}
    return False


def validate_plan(plan: object):
    if not isinstance(plan, list) or not plan:
        return False, "Manager plan must be a non-empty JSON list."
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


def validate_generated_files(files: object):
    if not isinstance(files, list) or not files:
        return False, "Coding output must be a non-empty JSON list."
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
        if Path(filename).name != filename:
            return False, f"Item {idx} filename must not include path segments."
        if Path(filename).suffix.lower() not in {".v", ".sv", ".vh", ".svh"}:
            return False, f"Item {idx} has unsupported extension."
        if not isinstance(file_info["content"], str) or not file_info["content"].strip():
            return False, f"Item {idx} has invalid content."
        content = file_info["content"]
        if any(marker in content for marker in ("<<<<<<<", "=======", ">>>>>>>")):
            return False, f"Item {idx} contains unresolved conflict markers."
        if Path(filename).suffix.lower() in {".v", ".sv"} and not re.search(
            r"\b(module|interface|package|primitive)\b", content
        ):
            return False, f"Item {idx} source file must contain a Verilog/SystemVerilog design unit."
    return True, ""


def parse_args():
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
        default=str(ARTIFACT_DIR),
        help="Directory for generated artifacts and logs.",
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
        "--llm-provider",
        choices=["ollama", "gpt-oss"],
        help="LLM provider/backend. 'gpt-oss' supports Ollama or OpenAI-compatible endpoints.",
    )
    parser.add_argument("--llm-model", help="LLM model name, for example gpt-oss:20b.")
    parser.add_argument("--llm-temperature", type=float, help="LLM temperature.")
    parser.add_argument(
        "--llm-api-url",
        help="OpenAI-compatible chat completions URL, for example http://abc.net:30001/chat/completions.",
    )
    parser.add_argument("--llm-api-key", help="API key for an OpenAI-compatible gpt-oss endpoint.")
    return parser.parse_args()


def read_user_requirement(raw_input: str) -> str:
    if raw_input.startswith("@"):
        raw_input = raw_input[1:].strip()

    possible_path = Path(raw_input).expanduser()
    if possible_path.is_file():
        return possible_path.read_text()
    return raw_input


def write_text_artifact(relative_path: str, content: str):
    path = ARTIFACT_DIR / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


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


def write_compile_order(files: List[Dict[str, str]]):
    compile_order = "\n".join(file_info["filename"] for file_info in files)
    write_text_artifact("compile_order.f", compile_order + ("\n" if compile_order else ""))


def basic_rtl_sanity(files: List[Dict[str, str]]) -> Dict[str, object]:
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
        if instance_module not in defined_modules:
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


def current_manager_task(state: AgentState):
    return state["manager_plan"][state["current_task_index"]]


def run_syntax_lint(files: List[Dict[str, str]], require_tool: bool = False) -> Dict[str, object]:
    if not files:
        return {"passed": False, "report": "No files were available for lint."}

    lint_tool = shutil.which("verilator") or shutil.which("iverilog")
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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return {
                "passed": False,
                "report": f"{Path(lint_tool).name} timed out after 30 seconds.",
            }
        output = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode == 0:
            return {"passed": True, "report": f"{Path(lint_tool).name} PASS"}
        return {
            "passed": False,
            "report": f"{Path(lint_tool).name} FAIL\n{output}",
        }


# --- 4. Define Agents ---
def intake_agent(state: AgentState):
    print("---MANAGER: Reading User Requirement---")
    user_request_input = state.get("user_request", "").strip()
    if not user_request_input:
        user_request_input = input(
            "Describe the RTL you want to build, or enter a spec file path / @path (or 'exit'): "
        ).strip()
    if user_request_input.lower() == "exit":
        sys.exit("Exiting.")
    user_request = read_user_requirement(user_request_input)
    write_text_artifact("user_requirement.txt", user_request)
    write_json_artifact("llm_config.json", active_llm_config)
    return {
        "user_request": user_request,
        "messages": [HumanMessage(content=f"User RTL requirement: {user_request}")],
    }


def manager_agent(state: AgentState):
    print("---MANAGER: Creating Top-Level RTL Plan---")
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Manager for a Verilog RTL coding organization.
Read the user's requirement and split it into ordered implementation tasks.

Rules:
- Keep the plan incremental. Each task should build on previous RTL.
- Preserve every concrete user requirement. Do not summarize away widths, protocols, timing, reset polarity, register behavior, names, or corner cases.
- Include architecture, interfaces, datapath/control logic, reset behavior, and verification readiness when relevant.
- Each task must be a complete handoff packet for the Supervisor, not just a short title.
- If a detail is unknown, write "TBD" instead of inventing it.
- Do not write code here.
- Return only raw JSON: a list of objects.
- Every object must include id, title, goal, deliverable.
- Add these fields whenever applicable:
  user_requirement_trace, dependencies, interfaces, parameters, control_logic,
  datapath, state_registers, reset_clocking, behavior, edge_cases,
  acceptance_criteria, notes.
""",
            ),
            ("human", "User requirement:\n{user_request}"),
        ]
    )
    response = (prompt | llm).invoke({"user_request": state["user_request"]})

    try:
        plan = _load_json(response.content)
        is_valid, validation_error = validate_plan(plan)
        if not is_valid:
            raise ValueError(validation_error)
        print(f"---MANAGER: Planned {len(plan)} tasks.---")
        write_json_artifact("manager_plan.json", plan)
        return {
            "manager_plan": plan,
            "current_task_index": 0,
            "messages": [response],
            "error_message": "",
        }
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"---ERROR: Manager produced invalid plan: {exc}---")
        fallback_plan = [
            {
                "id": "T1",
                "title": "Implement requested RTL",
                "goal": state["user_request"],
                "user_requirement_trace": state["user_request"],
                "dependencies": "TBD: Manager JSON recovery fallback used.",
                "interfaces": "TBD: derive exact ports, widths, and handshakes from the user requirement.",
                "parameters": "TBD: derive configurable widths/depths and defaults from the user requirement.",
                "control_logic": "TBD: identify FSMs, enables, valid/ready, done/error, and sequencing.",
                "datapath": "TBD: identify registers, counters, arithmetic, memories/FIFOs, muxes, and width policy.",
                "state_registers": "TBD: identify state and datapath registers with reset values.",
                "reset_clocking": "TBD: identify clock domains, reset polarity, reset values, and reset release behavior.",
                "behavior": state["user_request"],
                "edge_cases": "TBD: identify boundary values, simultaneous events, overflow/underflow, invalid inputs, and backpressure.",
                "acceptance_criteria": "Generated RTL must satisfy the original user requirement and pass sanity, lint when available, microarchitecture review, and verification review.",
                "deliverable": "Complete synthesizable Verilog/SystemVerilog RTL.",
                "notes": "Fallback plan created because Manager output was not valid structured JSON.",
            }
        ]
        write_json_artifact("manager_plan.json", fallback_plan)
        return {
            "manager_plan": fallback_plan,
            "current_task_index": 0,
            "messages": [response],
            "error_message": f"Manager plan fallback used: {exc}",
        }


def architecture_agent(state: AgentState):
    print("---ARCHITECT: Creating RTL Architecture Contract---")
    review_feedback = ""
    if state.get("architecture_review_report"):
        review_feedback = (
            "\nPrevious architecture review feedback to fix:\n"
            f"{state['architecture_review_report']}"
        )
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the RTL Architect.
Create an architecture contract that all later agents must follow.

Include:
- Proposed top module name and purpose.
- Clock/reset assumptions and reset polarity.
- External interface summary.
- Module decomposition table: module name, responsibility, inputs, outputs, parameters.
- Interface contract table: signal name, direction, width, clock domain, reset value, timing meaning.
- Key internal blocks with explicit control logic and datapath responsibilities.
- Expected FSMs, counters, registers, muxes, comparators, arithmetic units, and handshakes.
- Pipeline/latency/throughput assumptions.
- Clock-domain and reset-domain assumptions.
- Error, saturation, overflow/underflow, invalid input, and backpressure behavior.
- Parameterization policy.
- Coding constraints for synthesizable RTL.
- Architecture traceability matrix mapping user requirements and Manager tasks to architecture decisions.
- Open questions/TBD list. Do not hide unknowns.
- Verification intent, corner cases, and acceptance criteria.

Return concise Markdown.
""",
            ),
            (
                "human",
                """
User requirement:
{user_request}

Manager plan:
{manager_plan}

Manager handoff details:
{manager_handoff}

{review_feedback}
""",
            ),
        ]
    )
    response = (prompt | llm).invoke(
        {
            "user_request": state["user_request"],
            "manager_plan": render_manager_plan(state["manager_plan"]),
            "manager_handoff": (
                "Full Manager handoff:\n"
                f"{render_manager_plan(state['manager_plan'])}\n\n"
                "Original user requirement, authoritative source:\n"
                f"{state['user_request']}"
            ),
            "review_feedback": review_feedback,
        }
    )
    write_text_artifact("architecture_contract.md", response.content)
    return {
        "architecture_contract": response.content,
        "architecture_review_passed": False,
        "messages": [response],
    }


def architecture_review_agent(state: AgentState):
    print("---ARCHITECTURE REVIEW: Checking architecture contract completeness---")
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Architecture Review Gate.
Check whether the architecture contract is complete enough for Supervisor, Control/Data Path Planner, Coding Team, and Verification Team.

Review against:
- Original user requirement.
- Full Manager handoff.
- Manager task sequence.

Required architecture coverage:
- Top module and module decomposition.
- External interfaces with direction, width, timing meaning, and reset value.
- Clock/reset assumptions and domains.
- Control/data path responsibilities.
- FSM/counter/register/mux/arithmetic/memory resources.
- Latency, throughput, handshakes, backpressure.
- Error/overflow/underflow/invalid input behavior.
- Parameterization policy.
- Requirement-to-architecture traceability.
- Open TBDs clearly listed.
- Verification intent and acceptance criteria.

Return only raw JSON:
{{
  "pass": true|false,
  "report": "specific missing or weak architecture items to fix"
}}
""",
            ),
            (
                "human",
                """
Original user requirement:
{user_request}

Manager handoff:
{manager_handoff}

Architecture contract:
{architecture_contract}
""",
            ),
        ]
    )
    response = (prompt | llm).invoke(
        {
            "user_request": state["user_request"],
            "manager_handoff": (
                "Full Manager handoff:\n"
                f"{render_manager_plan(state['manager_plan'])}\n\n"
                "Original user requirement, authoritative source:\n"
                f"{state['user_request']}"
            ),
            "architecture_contract": state.get("architecture_contract") or "(none)",
        }
    )

    try:
        result = _load_json(response.content)
        passed = _json_bool(result.get("pass"))
        report = str(result.get("report", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        passed = False
        report = "Architecture review output was not valid JSON."

    write_text_artifact(
        f"logs/architecture_review_attempt_{state.get('architecture_retry_count', 0) + 1}.md",
        report or ("PASS" if passed else "FAIL"),
    )

    if passed:
        print("---ARCHITECTURE REVIEW: PASS---")
        return {
            "architecture_review_passed": True,
            "architecture_review_report": report or "PASS",
            "messages": [response],
        }

    print("---ARCHITECTURE REVIEW: FAIL---")
    return {
        "architecture_review_passed": False,
        "architecture_review_report": report or "Architecture contract is incomplete.",
        "architecture_retry_count": state.get("architecture_retry_count", 0) + 1,
        "messages": [response],
    }


def supervisor_agent(state: AgentState):
    task = current_manager_task(state)
    print(f"---SUPERVISOR: Detailing Task {task['id']} - {task['title']}---")
    review_feedback = ""
    if state.get("supervisor_review_report"):
        review_feedback = (
            "\nPrevious supervisor review feedback to fix:\n"
            f"{state['supervisor_review_report']}"
        )
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Supervisor for a Verilog RTL team.
Turn the Manager's current task into a concrete coding assignment.

Your output is the authoritative task packet for the downstream planner and coding team.
It must be concrete enough that the Coding Team can implement without guessing.

Include these Markdown sections:
1. Task Objective
   - Exact RTL behavior to implement now.
   - Explicit scope exclusions for this task.
2. Source Trace
   - Manager task id/title.
   - User requirement bullets this task satisfies.
   - Architecture contract decisions this task must obey.
3. File and Module Impact
   - Files/modules to create or modify.
   - Top/module interface changes, if any.
   - Compatibility constraints with existing RTL context.
4. Interface and Parameter Contract
   - Signal names, directions, widths, reset values, clock domains, handshake meanings.
   - Parameters/localparams and allowed values.
5. Control/Data Path Assignment
   - Control logic responsibilities.
   - Datapath responsibilities.
   - Registers, enables, mux selects, counters, valid/ready/done/error conditions.
6. Sequencing and Timing
   - Cycle-level behavior, latency, throughput, backpressure, reset release behavior.
7. Edge Cases and Error Handling
   - Overflow/underflow, invalid inputs, simultaneous events, boundary values.
8. Implementation Checklist
   - Concrete items the Coding Team must implement.
9. Verification Checklist
   - Concrete items the Verification Team must check.
10. Handoff Notes
   - TBDs, assumptions, and risks.

If information is unknown, mark it as TBD and explain why.
Do not write RTL code.
""",
            ),
            (
                "human",
                """
Original user requirement:
{user_request}

Full Manager plan:
{manager_plan}

Manager handoff packet:
{manager_handoff}

Architecture contract:
{architecture_contract}

Current Manager task:
{task}

Existing RTL context:
{rtl_context}

Previous verification report, if any:
{verification_report}

{review_feedback}
""",
            ),
        ]
    )
    response = (prompt | llm).invoke(
        {
            "user_request": state["user_request"],
            "manager_plan": render_manager_plan(state["manager_plan"]),
            "manager_handoff": current_manager_handoff(state),
            "architecture_contract": state.get("architecture_contract") or "(none)",
            "task": render_manager_task(task),
            "rtl_context": state.get("rtl_context") or "(none)",
            "verification_report": state.get("verification_report") or "(none)",
            "review_feedback": review_feedback,
        }
    )
    write_text_artifact(
        f"logs/{task['id']}_manager_handoff.md",
        current_manager_handoff(state),
    )
    write_text_artifact(
        f"logs/{task['id']}_supervisor_plan.md",
        response.content,
    )
    return {
        "supervisor_plan": response.content,
        "supervisor_review_passed": False,
        "generation_ok": False,
        "verification_passed": False,
        "messages": [response],
    }


def supervisor_review_agent(state: AgentState):
    task = current_manager_task(state)
    print(f"---SUPERVISOR REVIEW: Checking task packet for {task['id']}---")
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Supervisor Review Gate.
Check whether the Supervisor task packet is complete enough for the Control/Data Path Planner and Coding Team.

Required coverage:
- Task objective and explicit scope exclusions.
- Traceability to Manager handoff, user requirement, and architecture contract.
- Files/modules to create or modify.
- Interface/parameter contract with signal names, directions, widths, clock domains, reset values.
- Control/data path assignment.
- Cycle-level timing, latency, throughput, reset release, and backpressure behavior.
- Edge cases and error behavior.
- Implementation checklist.
- Verification checklist.
- TBDs/assumptions/risks called out explicitly.

Return only raw JSON:
{{
  "pass": true|false,
  "report": "specific missing or weak supervisor handoff items to fix"
}}
""",
            ),
            (
                "human",
                """
Original user requirement:
{user_request}

Manager handoff:
{manager_handoff}

Architecture contract:
{architecture_contract}

Supervisor task packet:
{supervisor_plan}
""",
            ),
        ]
    )
    response = (prompt | llm).invoke(
        {
            "user_request": state["user_request"],
            "manager_handoff": current_manager_handoff(state),
            "architecture_contract": state.get("architecture_contract") or "(none)",
            "supervisor_plan": state.get("supervisor_plan") or "(none)",
        }
    )

    try:
        result = _load_json(response.content)
        passed = _json_bool(result.get("pass"))
        report = str(result.get("report", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        passed = False
        report = "Supervisor review output was not valid JSON."

    write_text_artifact(
        f"logs/{task['id']}_supervisor_review_attempt_{state.get('supervisor_retry_count', 0) + 1}.md",
        report or ("PASS" if passed else "FAIL"),
    )

    if passed:
        print("---SUPERVISOR REVIEW: PASS---")
        return {
            "supervisor_review_passed": True,
            "supervisor_review_report": report or "PASS",
            "messages": [response],
        }

    print("---SUPERVISOR REVIEW: FAIL---")
    return {
        "supervisor_review_passed": False,
        "supervisor_review_report": report or "Supervisor task packet is incomplete.",
        "supervisor_retry_count": state.get("supervisor_retry_count", 0) + 1,
        "messages": [response],
    }


def control_datapath_planner_agent(state: AgentState):
    task = current_manager_task(state)
    print(f"---CONTROL/DATAPATH PLANNER: Structuring {task['id']}---")
    review_feedback = ""
    if state.get("control_datapath_review_report"):
        review_feedback = (
            "\nPrevious Control/Data Path review feedback to fix:\n"
            f"{state['control_datapath_review_report']}"
        )
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Control/Data Path Planner for a Verilog RTL team.
Before coding, create a concrete micro-architecture plan that cleanly separates control logic from datapath.

Include these sections:
1. Control Logic
   - FSM states and transitions, or explain why no FSM is needed.
   - Control outputs, enables, mux selects, valid/ready/done/error behavior.
   - Reset behavior for every control register.
2. Datapath
   - Data registers, counters, accumulators, memories/FIFOs, arithmetic/comparison units.
   - Data movement per cycle and mux/enable conditions.
   - Width/parameter choices and overflow/underflow handling.
3. Timing Contract
   - Latency, throughput, handshake assumptions, and backpressure handling.
4. Coding Guidance
   - Recommended always_comb/always_ff block structure.
   - Signals that should be separated into next-state, registered-state, control, and datapath groups.
5. Verification Focus
   - Specific corner cases the Verification Team must check for this task.
6. Implementation Checklist
   - Bullet list of concrete code features that must appear in the RTL.
   - Include expected signal names or naming patterns when useful.
   - Include what must be separated into control blocks and datapath blocks.

Return concise Markdown. Do not write RTL code.
""",
            ),
            (
                "human",
                """
Original user requirement:
{user_request}

Architecture contract:
{architecture_contract}

Current Manager task:
{task}

Manager handoff packet:
{manager_handoff}

Supervisor detailed assignment:
{supervisor_plan}

Existing RTL context:
{rtl_context}

Previous verification report, if any:
{verification_report}

{review_feedback}
""",
            ),
        ]
    )
    response = (prompt | llm).invoke(
        {
            "user_request": state["user_request"],
            "architecture_contract": state.get("architecture_contract") or "(none)",
            "task": render_manager_task(task),
            "manager_handoff": current_manager_handoff(state),
            "supervisor_plan": state["supervisor_plan"],
            "rtl_context": state.get("rtl_context") or "(none)",
            "verification_report": state.get("verification_report") or "(none)",
            "review_feedback": review_feedback,
        }
    )
    write_text_artifact(
        f"logs/{task['id']}_control_datapath_plan.md",
        response.content,
    )
    return {
        "control_datapath_plan": response.content,
        "control_datapath_review_passed": False,
        "messages": [response],
    }


def control_datapath_review_agent(state: AgentState):
    task = current_manager_task(state)
    print(f"---CONTROL/DATAPATH REVIEW: Checking micro-architecture plan for {task['id']}---")
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Control/Data Path Review Gate.
Check whether the micro-architecture plan is concrete enough for RTL coding.

Required coverage:
- FSM/state sequencing, or a clear reason no FSM is needed.
- Control outputs, enables, mux selects, valid/ready, done/error, load/clear.
- Datapath registers, counters, arithmetic/comparison units, memories/FIFOs, and muxes.
- Cycle-level timing, latency, throughput, reset release, and backpressure.
- Width and parameter policy, including overflow/underflow handling.
- Clear mapping from Supervisor assignment to implementation checklist.
- Verification focus with concrete corner cases.

Return only raw JSON:
{{
  "pass": true|false,
  "report": "specific missing or weak control/datapath plan items to fix"
}}
""",
            ),
            (
                "human",
                """
Original user requirement:
{user_request}

Architecture contract:
{architecture_contract}

Manager handoff:
{manager_handoff}

Supervisor task packet:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}
""",
            ),
        ]
    )
    response = (prompt | llm).invoke(
        {
            "user_request": state["user_request"],
            "architecture_contract": state.get("architecture_contract") or "(none)",
            "manager_handoff": current_manager_handoff(state),
            "supervisor_plan": state.get("supervisor_plan") or "(none)",
            "control_datapath_plan": state.get("control_datapath_plan") or "(none)",
        }
    )

    try:
        result = _load_json(response.content)
        passed = _json_bool(result.get("pass"))
        report = str(result.get("report", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        passed = False
        report = "Control/Data Path review output was not valid JSON."

    write_text_artifact(
        f"logs/{task['id']}_control_datapath_review_attempt_{state.get('control_datapath_retry_count', 0) + 1}.md",
        report or ("PASS" if passed else "FAIL"),
    )

    if passed:
        print("---CONTROL/DATAPATH REVIEW: PASS---")
        return {
            "control_datapath_review_passed": True,
            "control_datapath_review_report": report or "PASS",
            "messages": [response],
        }

    print("---CONTROL/DATAPATH REVIEW: FAIL---")
    return {
        "control_datapath_review_passed": False,
        "control_datapath_review_report": report or "Control/Data Path plan is incomplete.",
        "control_datapath_retry_count": state.get("control_datapath_retry_count", 0) + 1,
        "messages": [response],
    }


def verilog_coding_team_agent(state: AgentState):
    task = current_manager_task(state)
    print(f"---VERILOG CODING TEAM: Implementing {task['id']}---")
    feedback = ""
    if state.get("verification_report") and state.get("verification_retry_count", 0) > 0:
        feedback = f"\nFix the previous verification failures:\n{state['verification_report']}"
    if state.get("microarchitecture_report") and state.get("microarchitecture_retry_count", 0) > 0:
        feedback += f"\nFix the previous microarchitecture review failures:\n{state['microarchitecture_report']}"
    if state.get("error_message") and state.get("coding_retry_count", 0) > 0:
        feedback += f"\nFix the previous coding output format failure:\n{state['error_message']}"

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Verilog Coding Team.
Produce synthesizable RTL files for the Supervisor's assignment.

Rules:
- Prefer SystemVerilog only when it improves clarity; .sv and .v are both allowed.
- Keep modules synthesizable unless a file is clearly a package/header.
- Preserve existing module interfaces unless the Supervisor explicitly requires an extension.
- Implement the Control/Data Path plan faithfully.
- Separate control and datapath clearly in the code:
  - Use distinct next-state/current-state logic for FSMs.
  - Use explicit control signals for enables, mux selects, load/clear, valid/ready, done/error.
  - Keep datapath registers and arithmetic/comparison logic readable and grouped.
  - Avoid mixing unrelated state updates into one opaque always block.
- Prefer always_ff/always_comb in .sv files; if using .v, use equivalent clean sequential/combinational structure.
- Give every registered control and datapath signal an explicit reset or documented reason it does not need one.
- Include meaningful parameters and comments only where they clarify non-obvious logic.
- Return only raw JSON: a list of objects with keys filename and content.
- Each content value must contain the complete file content.
""",
            ),
            (
                "human",
                """
Original user requirement:
{user_request}

Architecture contract:
{architecture_contract}

Current Manager task:
{task}

Manager handoff packet:
{manager_handoff}

Supervisor detailed assignment:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}

Current RTL files:
{rtl_context}
{feedback}
""",
            ),
        ]
    )
    response = (prompt | llm).invoke(
        {
            "user_request": state["user_request"],
            "architecture_contract": state.get("architecture_contract") or "(none)",
            "task": render_manager_task(task),
            "manager_handoff": current_manager_handoff(state),
            "supervisor_plan": state["supervisor_plan"],
            "control_datapath_plan": state.get("control_datapath_plan") or "(none)",
            "rtl_context": state.get("rtl_context") or "(none)",
            "feedback": feedback,
        }
    )

    try:
        files = _load_json(response.content)
        is_valid, validation_error = validate_generated_files(files)
        if not is_valid:
            raise ValueError(validation_error)
        print(f"---VERILOG CODING TEAM: Generated {len(files)} candidate files.---")
        write_json_artifact(
            f"logs/{task['id']}_coding_attempt_{state.get('coding_retry_count', 0) + 1}.json",
            files,
        )
        return {
            "candidate_files": files,
            "generation_ok": True,
            "microarchitecture_passed": False,
            "messages": [response],
            "failed_stage": "",
            "blocking_report": "",
            "error_message": "",
        }
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"---ERROR: Coding team produced invalid JSON: {exc}---")
        write_text_artifact(
            f"failed_attempts/{task['id']}_invalid_json_attempt_{state.get('coding_retry_count', 0) + 1}.txt",
            response.content,
        )
        report = f"Coding output format failed: {exc}. Regenerate valid JSON only."
        return {
            "generation_ok": False,
            "microarchitecture_passed": False,
            "verification_passed": False,
            "verification_report": report,
            "coding_retry_count": state.get("coding_retry_count", 0) + 1,
            "failed_stage": "coding",
            "blocking_report": report,
            "messages": [response],
            "error_message": str(exc),
        }


def microarchitecture_reviewer_agent(state: AgentState):
    task = current_manager_task(state)
    print(f"---MICROARCH REVIEWER: Checking control/datapath implementation for {task['id']}---")
    merged_files = merge_files(state.get("final_files", []), state.get("candidate_files", []))
    static_result = static_microarchitecture_review(state.get("candidate_files", []))
    write_text_artifact(
        f"logs/{task['id']}_microarchitecture_static_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.txt",
        static_result["report"],
    )

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Microarchitecture Reviewer.
Review only whether the RTL implementation follows the Control/Data Path plan.

Focus:
- Control and datapath are visibly separated.
- FSM/current-state/next-state structure is clear when an FSM is required.
- Control outputs, enables, load/clear, mux selects, valid/ready/done/error are explicit.
- Datapath registers, counters, arithmetic/comparison units, and memories are grouped and readable.
- Reset behavior covers control state and datapath registers.
- Timing, latency, and backpressure assumptions from the plan are reflected in code.

Do not perform general functional verification here.
Return only raw JSON:
{{
  "pass": true|false,
  "report": "specific control/datapath implementation findings and required fixes"
}}
""",
            ),
            (
                "human",
                """
Architecture contract:
{architecture_contract}

Current Manager task:
{task}

Manager handoff packet:
{manager_handoff}

Supervisor detailed assignment:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}

Static microarchitecture scan:
{static_report}

RTL candidate:
{candidate_rtl}
""",
            ),
        ]
    )
    response = (prompt | llm).invoke(
        {
            "architecture_contract": state.get("architecture_contract") or "(none)",
            "task": render_manager_task(task),
            "manager_handoff": current_manager_handoff(state),
            "supervisor_plan": state["supervisor_plan"],
            "control_datapath_plan": state.get("control_datapath_plan") or "(none)",
            "static_report": static_result["report"],
            "candidate_rtl": render_files(merged_files),
        }
    )

    try:
        result = _load_json(response.content)
        passed = _json_bool(result.get("pass")) and static_result["passed"]
        report = str(result.get("report", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        passed = False
        report = "Microarchitecture review output was not valid JSON."

    if not static_result["passed"]:
        report = f"Static microarchitecture scan failed:\n{static_result['report']}\n\n{report}"

    write_text_artifact(
        f"logs/{task['id']}_microarchitecture_review_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.md",
        report or ("PASS" if passed else "FAIL"),
    )

    if passed:
        print("---MICROARCH REVIEWER: PASS---")
        return {
            "microarchitecture_passed": True,
            "microarchitecture_report": report or "PASS",
            "failed_stage": "",
            "blocking_report": "",
            "messages": [response],
        }

    print("---MICROARCH REVIEWER: FAIL---")
    write_text_artifact(
        f"failed_attempts/{task['id']}_microarchitecture_failed_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.txt",
        render_files(state.get("candidate_files", [])) + "\n\n" + report,
    )
    return {
        "microarchitecture_passed": False,
        "microarchitecture_report": report or "Microarchitecture review failed.",
        "microarchitecture_retry_count": state.get("microarchitecture_retry_count", 0) + 1,
        "failed_stage": "microarchitecture_review",
        "blocking_report": report or "Microarchitecture review failed.",
        "messages": [response],
    }


def verification_team_agent(state: AgentState):
    task = current_manager_task(state)
    print(f"---VERIFICATION TEAM: Checking {task['id']}---")
    merged_files = merge_files(state.get("final_files", []), state.get("candidate_files", []))
    sanity_result = basic_rtl_sanity(merged_files)
    write_text_artifact(
        f"logs/{task['id']}_basic_sanity_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
        sanity_result["report"],
    )
    if not sanity_result["passed"]:
        report = f"Basic RTL sanity failed before lint:\n{sanity_result['report']}"
        write_text_artifact(
            f"failed_attempts/{task['id']}_sanity_failed_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
            render_files(state.get("candidate_files", [])) + "\n\n" + report,
        )
        print("---VERIFICATION TEAM: BASIC SANITY FAIL---")
        return {
            "verification_passed": False,
            "verification_report": report,
            "verification_retry_count": state.get("verification_retry_count", 0) + 1,
            "failed_stage": "verification",
            "blocking_report": report,
        }

    lint_result = run_syntax_lint(merged_files, state.get("require_lint", False))
    write_text_artifact(
        f"logs/{task['id']}_lint_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
        lint_result["report"],
    )
    if not lint_result["passed"]:
        report = f"Syntax lint failed before functional review:\n{lint_result['report']}"
        write_text_artifact(
            f"failed_attempts/{task['id']}_lint_failed_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
            render_files(state.get("candidate_files", [])) + "\n\n" + report,
        )
        print("---VERIFICATION TEAM: LINT FAIL---")
        return {
            "verification_passed": False,
            "verification_report": report,
            "lint_report": lint_result["report"],
            "verification_retry_count": state.get("verification_retry_count", 0) + 1,
            "failed_stage": "verification_lint",
            "blocking_report": report,
        }

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Verification Team for a Verilog RTL coding organization.
Review the candidate RTL against the user requirement, Manager task, and Supervisor assignment.

Check:
- Synthesizability and obvious syntax issues.
- Module/interface consistency across files.
- Clock/reset behavior.
- State-machine and datapath correctness.
- Whether control logic is cleanly separated from datapath logic.
- Whether FSM states/transitions, enables, mux selects, counters, datapath registers, and handshakes match the Control/Data Path plan.
- Whether datapath width choices, overflow/underflow behavior, and reset values are sensible.
- Whether the current task is satisfied without breaking previous RTL context.

Return only raw JSON with:
{{
  "pass": true|false,
  "report": "concise verification result and required fixes"
}}
""",
            ),
            (
                "human",
                """
Original user requirement:
{user_request}

Architecture contract:
{architecture_contract}

Current Manager task:
{task}

Manager handoff packet:
{manager_handoff}

Supervisor detailed assignment:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}

RTL candidate to verify:
{candidate_rtl}
""",
            ),
        ]
    )
    response = (prompt | llm).invoke(
        {
            "user_request": state["user_request"],
            "architecture_contract": state.get("architecture_contract") or "(none)",
            "task": render_manager_task(task),
            "manager_handoff": current_manager_handoff(state),
            "supervisor_plan": state["supervisor_plan"],
            "control_datapath_plan": state.get("control_datapath_plan") or "(none)",
            "candidate_rtl": render_files(merged_files),
        }
    )

    try:
        result = _load_json(response.content)
        passed = _json_bool(result.get("pass"))
        report = str(result.get("report", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        passed = False
        report = "Verification output was not valid JSON. Re-run coding with clearer, self-checkable RTL."

    if passed:
        print("---VERIFICATION TEAM: PASS---")
        write_text_artifact(
            f"logs/{task['id']}_verification_report.md",
            report or "PASS",
        )
        return {
            "verification_passed": True,
            "verification_report": report or "PASS",
            "lint_report": lint_result["report"],
            "failed_stage": "",
            "blocking_report": "",
            "messages": [response],
        }

    print("---VERIFICATION TEAM: FAIL---")
    write_text_artifact(
        f"failed_attempts/{task['id']}_functional_failed_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
        render_files(state.get("candidate_files", [])) + "\n\n" + report,
    )
    return {
        "verification_passed": False,
        "verification_report": report or "Verification failed without a detailed report.",
        "lint_report": lint_result["report"],
        "verification_retry_count": state.get("verification_retry_count", 0) + 1,
        "failed_stage": "verification",
        "blocking_report": report or "Verification failed without a detailed report.",
        "messages": [response],
    }


def supervisor_accept_agent(state: AgentState):
    task = current_manager_task(state)
    print(f"---SUPERVISOR: Accepting {task['id']} and Preparing Next Task---")
    final_files = merge_files(state.get("final_files", []), state.get("candidate_files", []))
    rtl_context = render_files(final_files)
    top_module_candidates = extract_module_names(final_files)
    write_json_artifact(f"logs/{task['id']}_accepted_files.json", final_files)
    write_json_artifact("logs/top_module_candidates.json", top_module_candidates)
    return {
        "final_files": final_files,
        "rtl_context": rtl_context,
        "top_module_candidates": top_module_candidates,
        "current_task_index": state["current_task_index"] + 1,
        "candidate_files": [],
        "coding_retry_count": 0,
        "microarchitecture_retry_count": 0,
        "verification_retry_count": 0,
        "generation_ok": False,
        "verification_passed": False,
        "verification_report": "",
        "lint_report": "",
        "supervisor_plan": "",
        "supervisor_review_passed": False,
        "supervisor_review_report": "",
        "supervisor_retry_count": 0,
        "control_datapath_plan": "",
        "control_datapath_review_passed": False,
        "control_datapath_review_report": "",
        "control_datapath_retry_count": 0,
        "microarchitecture_passed": False,
        "microarchitecture_report": "",
        "failed_stage": "",
        "blocking_report": "",
    }


def testbench_team_agent(state: AgentState):
    print("---TESTBENCH TEAM: Creating Smoke Testbench---")
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Verilog Testbench Team.
Create a lightweight smoke testbench for the accepted RTL.

Rules:
- The testbench may be non-synthesizable.
- Instantiate the most likely top module from the RTL context.
- Generate clock/reset stimulus when ports indicate clock/reset.
- Drive simple deterministic stimulus and finish the simulation.
- Return only raw JSON: a list of objects with keys filename and content.
""",
            ),
            (
                "human",
                """
Original user requirement:
{user_request}

Accepted RTL files:
{rtl_context}

Top module candidates, in observed order:
{top_module_candidates}
""",
            ),
        ]
    )
    response = (prompt | llm).invoke(
        {
            "user_request": state["user_request"],
            "rtl_context": render_files(state.get("final_files", [])),
            "top_module_candidates": ", ".join(state.get("top_module_candidates", [])) or "(unknown)",
        }
    )

    try:
        files = _load_json(response.content)
        is_valid, validation_error = validate_generated_files(files)
        if not is_valid:
            raise ValueError(validation_error)
        write_json_artifact("logs/testbench_files.json", files)
        return {
            "testbench_files": files,
            "generation_ok": True,
            "failed_stage": "",
            "blocking_report": "",
            "messages": [response],
        }
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"---ERROR: Testbench team produced invalid JSON: {exc}---")
        write_text_artifact(
            f"failed_attempts/testbench_invalid_json_attempt_{state.get('testbench_retry_count', 0) + 1}.txt",
            response.content,
        )
        report = f"Testbench generation failed: {exc}"
        return {
            "testbench_files": [],
            "generation_ok": False,
            "verification_report": report,
            "testbench_retry_count": state.get("testbench_retry_count", 0) + 1,
            "failed_stage": "testbench",
            "blocking_report": report,
            "messages": [response],
        }


def final_lint_agent(state: AgentState):
    print("---FINAL LINT: Checking RTL and Testbench Together---")
    all_files = state.get("final_files", []) + state.get("testbench_files", [])
    lint_result = run_syntax_lint(all_files, state.get("require_lint", False))
    write_text_artifact("logs/final_lint_report.txt", lint_result["report"])
    if lint_result["passed"]:
        return {
            "final_lint_passed": True,
            "final_lint_report": lint_result["report"],
            "failed_stage": "",
            "blocking_report": "",
        }
    return {
        "final_lint_passed": False,
        "final_lint_report": lint_result["report"],
        "failed_stage": "final_lint",
        "blocking_report": lint_result["report"],
    }


def final_review_agent(state: AgentState):
    if os.getenv("AUTO_APPROVE_FINAL", "").strip().lower() in {"1", "true", "yes"}:
        return {"human_approved": True, "run_status": "passed"}

    all_files = state.get("final_files", []) + state.get("testbench_files", [])
    print("\n" + "-" * 20 + " FINAL REVIEW " + "-" * 20)
    print(f"Top module candidates: {', '.join(state.get('top_module_candidates', [])) or '(unknown)'}")
    print(f"Final lint: {state.get('final_lint_report') or '(not run)'}")
    print(render_files(all_files))
    print("\n" + "-" * 54)
    feedback = input("Write files? (approve / reject): ").strip().lower()
    if feedback == "approve":
        write_text_artifact("logs/final_human_approval.txt", "approve")
        return {"human_approved": True, "run_status": "passed"}

    write_text_artifact("logs/final_human_approval.txt", feedback or "reject")
    return {
        "human_approved": False,
        "run_status": "failed",
        "failed_stage": "final_review",
        "blocking_report": f"Final human approval rejected: {feedback or 'reject'}",
        "verification_report": f"Final human approval rejected: {feedback or 'reject'}",
    }


def summary_agent(state: AgentState):
    print("---SUMMARY: Writing Run Summary---")
    failed_stage = state.get("failed_stage", "")
    run_status = state.get("run_status", "")
    if not run_status:
        if state.get("human_approved"):
            run_status = "passed"
        elif failed_stage:
            run_status = "failed"
        else:
            run_status = "not_written"
    blocking_report = (
        state.get("blocking_report")
        or state.get("verification_report")
        or state.get("final_lint_report")
        or ""
    )
    summary = {
        "run_status": run_status,
        "failed_stage": failed_stage,
        "blocking_report": blocking_report,
        "artifact_dir": str(ARTIFACT_DIR),
        "llm_config": active_llm_config,
        "llm_config_saved": str(ARTIFACT_DIR / "llm_config.json"),
        "user_request_saved": str(ARTIFACT_DIR / "user_requirement.txt"),
        "manager_plan_saved": str(ARTIFACT_DIR / "manager_plan.json"),
        "architecture_contract_saved": str(ARTIFACT_DIR / "architecture_contract.md"),
        "last_architecture_review_report": state.get("architecture_review_report", ""),
        "last_supervisor_review_report": state.get("supervisor_review_report", ""),
        "control_datapath_plans_saved": str(ARTIFACT_DIR / "logs"),
        "last_microarchitecture_report": state.get("microarchitecture_report", ""),
        "rtl_files": [file_info["filename"] for file_info in state.get("final_files", [])],
        "testbench_files": [
            file_info["filename"] for file_info in state.get("testbench_files", [])
        ],
        "compile_order_saved": str(ARTIFACT_DIR / "compile_order.f"),
        "file_manifest_saved": str(ARTIFACT_DIR / "file_manifest.json"),
        "top_module_candidates": state.get("top_module_candidates", []),
        "human_approved": state.get("human_approved", False),
        "last_verification_report": state.get("verification_report", ""),
        "last_lint_report": state.get("lint_report", ""),
        "require_lint": state.get("require_lint", False),
        "final_lint_passed": state.get("final_lint_passed", False),
        "final_lint_report": state.get("final_lint_report", ""),
        "retry_counts": {
            "architecture": state.get("architecture_retry_count", 0),
            "supervisor": state.get("supervisor_retry_count", 0),
            "control_datapath": state.get("control_datapath_retry_count", 0),
            "coding": state.get("coding_retry_count", 0),
            "microarchitecture": state.get("microarchitecture_retry_count", 0),
            "verification": state.get("verification_retry_count", 0),
            "testbench": state.get("testbench_retry_count", 0),
        },
        "retry_limits": {
            "architecture": state.get("max_architecture_retries", 0),
            "supervisor": state.get("max_supervisor_retries", 0),
            "control_datapath": state.get("max_control_datapath_retries", 0),
            "coding": state.get("max_retries", 0),
            "microarchitecture": state.get("max_retries", 0),
            "verification": state.get("max_retries", 0),
            "testbench": state.get("max_testbench_retries", 0),
        },
        "logs_dir": str(ARTIFACT_DIR / "logs"),
        "failed_attempts_dir": str(ARTIFACT_DIR / "failed_attempts"),
    }
    all_files = state.get("final_files", []) + state.get("testbench_files", [])
    write_compile_order(all_files)
    write_json_artifact("file_manifest.json", build_file_manifest(all_files))
    write_json_artifact("run_summary.json", summary)
    return {}


def writer_agent(state: AgentState):
    print("---WRITER: Writing Final RTL Files---")
    files = state.get("final_files", []) + state.get("testbench_files", [])
    if not files:
        print("---ERROR: No files to write.---")
        return {}

    tool_calls = []
    for idx, file_info in enumerate(files):
        tool_calls.append(
            {
                "id": f"tool_call_writer_{idx}",
                "name": "write_verilog_file",
                "args": {
                    "filename": file_info["filename"],
                    "content": file_info["content"],
                },
            }
        )
    return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}


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
workflow.add_edge("manager", "architecture")
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
    ARTIFACT_DIR = Path(args.artifact_dir)
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

    print("Verilog Coding Agent (Manager / Supervisor / Coding / Verification)")
    print(
        f"LLM: provider={active_llm_config['provider']} "
        f"backend={active_llm_config['backend']} model={active_llm_config['model']}"
    )
    initial_state = {
        "messages": [],
        "user_request": args.spec or "",
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
        "final_lint_passed": False,
        "final_lint_report": "",
        "human_approved": False,
        "skip_testbench": args.no_testbench,
        "max_retries": args.max_retries,
        "run_status": "",
        "failed_stage": "",
        "blocking_report": "",
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
