from __future__ import annotations

import json
import re

from .runtime import refresh_globals


def _with_runtime(fn):
    def wrapped(*args, **kwargs):
        refresh_globals(globals())
        return fn(*args, **kwargs)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


def _load_coding_prompt(filename: str) -> str:
    return f"{load_prompt('coding_runtime_contract.md')}\n\n{load_prompt(filename)}"


def _strip_hdl_comments(content: str) -> str:
    runtime_strip = globals().get("strip_hdl_comments")
    if callable(runtime_strip):
        return runtime_strip(content)
    return re.sub(r"//.*?$|/\*.*?\*/", "", content, flags=re.S | re.M)


LOCAL_CODING_GATE_STAGES = {
    "coding",
    "coding_repair_contract",
    "coding_review_gate",
    "coding_preflight",
}


def _code_free_status_message(stage: str, task_id: str, detail: str) -> AIMessage:
    return AIMessage(
        content=(
            f"{stage} for {task_id}: {detail} "
            "Generated Verilog code is carried only in structured file fields and artifacts."
        )
    )


def _review_feedback_for_coding(state: AgentState, max_chars: int) -> str:
    return render_coding_repair_backlog(state, max_chars)


def _coding_feedback_entries(state: AgentState) -> list[dict[str, str]]:
    return active_coding_feedback_entries(state)


def _render_previous_candidate_manifest(files: list[dict[str, str]]) -> str:
    if not files:
        return "(none)"
    lines = []
    for item in build_file_manifest(files):
        sha = str(item.get("sha256", ""))
        lines.append(
            f"- {item.get('filename')}: {item.get('bytes')} bytes, sha256={sha[:12]}"
        )
    return "\n".join(lines)


def _render_coding_revision_plan(state: AgentState, max_chars: int) -> str:
    entries = _coding_feedback_entries(state)
    if not entries:
        return "(none)"

    lines = [
        "Reviewer-driven RTL revision checklist:",
        "- Treat every item below as a blocking fix unless it explicitly says it is informational.",
        "- Update the previous candidate RTL directly; keep module interfaces stable unless the feedback requires a change.",
        "- Return full revised Verilog-2001 files only after every checklist item is addressed.",
        "",
    ]
    for idx, entry in enumerate(entries[-8:], start=1):
        report = str(entry.get("report", "")).strip() or "No detailed report."
        lines.append(
            f"{idx}. stage={entry.get('stage', 'unknown')} task={entry.get('task_id', 'global')}"
        )
        lines.append(f"   required_fix: {report}")
    return clip_text("\n".join(lines), max_chars)


def _infer_repair_focus_tags(report: str) -> list[str]:
    lowered = report.lower()
    checks = [
        (("reset", "rst", "초기화"), "reset/initialization"),
        (("fsm", "state", "상태"), "fsm/state transition"),
        (("done", "valid", "ready", "handshake", "완료"), "completion/handshake"),
        (("counter", "count", "timer", "카운터"), "counter/timer"),
        (("overflow", "underflow", "saturat"), "numeric bounds"),
        (("width", "trunc", "extend", "signed", "폭"), "bit width/signing"),
        (("latch", "combinational", "default", "조합"), "combinational defaults"),
        (("datapath", "mux", "adder", "compare", "데이터"), "datapath operation"),
        (("control", "enable", "load", "clear", "제어"), "control signal"),
        (("interface", "port", "input", "output", "포트"), "module interface"),
        (("syntax", "semicolon", "endmodule", "compile", "lint"), "syntax/lint"),
        (("verilog", "systemverilog", "logic", "always_ff"), "Verilog-2001 compliance"),
    ]
    tags = []
    for needles, tag in checks:
        if any(needle in lowered for needle in needles):
            tags.append(tag)
    return tags or ["functional behavior"]


def _render_targeted_repair_brief(state: AgentState, max_chars: int) -> str:
    entries = _coding_feedback_entries(state)
    if not entries:
        return "(none)"

    lines = [
        "Targeted repair brief:",
        "- For each finding, identify the affected control/data path behavior before editing.",
        "- Prefer a real RTL behavior change over comments, formatting, or unrelated rewrites.",
        "- After editing, mentally re-run the failing reviewer scenario and ensure the report would no longer apply.",
        "",
    ]
    for idx, entry in enumerate(entries[-8:], start=1):
        report = str(entry.get("report", "")).strip() or "No detailed report."
        tags = ", ".join(_infer_repair_focus_tags(report))
        lines.append(
            f"{idx}. focus={tags}; stage={entry.get('stage', 'unknown')}; "
            f"task={entry.get('task_id', 'global')}"
        )
        lines.append(f"   repair_goal: {report}")
    return clip_text("\n".join(lines), max_chars)


def _render_local_gate_feedback(state: AgentState, max_chars: int) -> str:
    failed_stage = str(state.get("failed_stage") or "")
    if failed_stage not in LOCAL_CODING_GATE_STAGES:
        return "(none)"
    report = str(
        state.get("blocking_report")
        or state.get("error_message")
        or state.get("verification_report")
        or ""
    ).strip()
    if not report:
        return "(none)"
    lines = [
        "Most recent local Coding Team gate failure:",
        f"- failed_stage: {failed_stage}",
        "- This is not external reviewer backlog, but it is a blocking instruction for the next coding retry.",
        "- The next RTL candidate must directly eliminate this local gate failure.",
        "- Make only the complete functional edits required by the supplied evidence and acceptance conditions.",
        "",
        report,
    ]
    return clip_text("\n".join(lines), max_chars)


def _extract_module_headers(files: list[dict[str, str]], max_chars: int) -> str:
    headers = []
    module_pattern = re.compile(
        r"\bmodule\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b(?P<header>.*?);",
        flags=re.S,
    )
    for file_info in files:
        filename = str(file_info.get("filename", "")).strip()
        content = _strip_hdl_comments(str(file_info.get("content", "")))
        for match in module_pattern.finditer(content):
            header = re.sub(r"\s+", " ", match.group(0)).strip()
            headers.append(f"- {filename}: {header}")
    if not headers:
        return "(no module headers extracted)"
    return clip_text("\n".join(headers), max_chars)


def _render_previous_candidate_for_coding(state: AgentState, max_chars: int) -> str:
    previous_files = state.get("candidate_files", [])
    return render_files_for_prompt(previous_files, max_chars)


def _render_implementation_obligation_packet(
    state: AgentState, task: dict, max_chars: int
) -> str:
    entries = _coding_feedback_entries(state)
    review_backlog = render_coding_repair_backlog(state, max_chars)
    local_gate_feedback = _render_local_gate_feedback(state, max_chars)
    chunk_limit = max(800, max_chars // 6)
    lines = [
        "Current architecture/review implementation obligations:",
        "Apply the source authority and retry rules from coding_runtime_contract.md.",
        "",
        "Architecture contract obligations to preserve in RTL:",
        clip_text(state.get("architecture_contract") or "(none)", chunk_limit),
        "",
        "Supervisor assignment obligations to implement:",
        clip_text(state.get("supervisor_plan") or "(none)", chunk_limit),
        "",
        "Control/Data Path plan obligations to implement:",
        clip_text(state.get("control_datapath_plan") or "(none)", chunk_limit),
        "",
        "Reviewer-driven RTL change requests to close:",
        review_backlog,
        "",
        "Latest local coding gate feedback to close:",
        local_gate_feedback,
        "",
        "Required implementation response:",
        "- Update the affected modules/files directly; preserve required file names and module interfaces unless a review item requires a change.",
        "- Make control logic explicit: state registers, next-state logic, enables, load/clear, done/valid/ready, and error handling when applicable.",
        "- Make datapath explicit: registers, mux/select behavior, arithmetic/comparison paths, width/sign handling, and output registers when applicable.",
        "- Align reset behavior with the architecture and reviews for every affected register.",
        "- Return complete synthesizable Verilog-2001 files only after all listed obligations are reflected in code.",
    ]
    if entries:
        lines.extend(["", "Per-review closure checklist:"])
        for idx, entry in enumerate(entries[-10:], start=1):
            report = str(entry.get("report", "")).strip() or "No detailed report."
            tags = ", ".join(_infer_repair_focus_tags(report))
            lines.append(
                f"{idx}. stage={entry.get('stage', 'unknown')} "
                f"task={entry.get('task_id', 'global')} focus={tags}"
            )
            lines.append(f"   required RTL change: {report}")
            lines.append("   closure evidence: this finding must be obsolete in the returned Verilog.")
    return clip_text("\n".join(lines), max_chars)


def _coding_revision_mode(state: AgentState) -> str:
    attempt = state.get("coding_retry_count", 0) + 1
    if _coding_feedback_entries(state):
        return (
            f"review-driven retry attempt {attempt}; revise the previous candidate RTL "
            "to close reviewer findings before generating output"
        )
    if state.get("candidate_files"):
        return f"revision attempt {attempt}; use the previous candidate as the starting point"
    return f"fresh implementation attempt {attempt}"


def _task_contract_field(task: dict, *names: str) -> str:
    for name in names:
        value = task.get(name)
        if value not in (None, "", [], {}):
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            return str(value).strip()
    return "(not explicitly specified)"


def _render_rtl_quality_contract(state: AgentState, task: dict, max_chars: int) -> str:
    lines = [
        "RTL implementation quality contract:",
        "- Functional behavior and cycle timing must follow the task and reviewed plans exactly; do not invent ports, protocols, or latency.",
        "- Every sequential register must have intentional reset and hold/update behavior. Use nonblocking assignments in clocked blocks.",
        "- Every combinational output/next-state/control signal must be assigned on all paths, with defaults and a default case where applicable.",
        "- Keep control and datapath responsibilities explicit: FSM/handshake/enables select when work happens; datapath registers/arithmetic define what data changes.",
        "- Derive terminal counts, comparisons, slices, extensions, and arithmetic widths explicitly. Avoid silent truncation and signed/unsigned ambiguity.",
        "- For ready/valid, start/done, request/acknowledge, or similar protocols, define acceptance, persistence, busy behavior, and back-to-back transactions when applicable.",
        "- Handle boundary conditions named by the task, including reset during activity, minimum/maximum values, overflow policy, zero-length work, and simultaneous controls when applicable.",
        "- Use synthesizable Verilog-2001 only and return complete files with stable interfaces unless a reviewed requirement explicitly changes them.",
        "",
        f"Required behavior: {_task_contract_field(task, 'behavior', 'goal', 'description')}",
        f"Cycle/latency expectations: {_task_contract_field(task, 'timing', 'latency', 'cycle_behavior')}",
        f"Reset/clocking expectations: {_task_contract_field(task, 'reset_clocking', 'reset', 'clocking')}",
        f"Edge cases: {_task_contract_field(task, 'edge_cases', 'corner_cases')}",
        f"Acceptance criteria: {_task_contract_field(task, 'acceptance_criteria', 'acceptance', 'deliverable')}",
    ]
    return clip_text("\n".join(lines), max_chars)


def _coding_repair_intensity(state: AgentState) -> str:
    attempts = max(
        state.get("coding_retry_count", 0),
        state.get("microarchitecture_retry_count", 0),
        state.get("verification_retry_count", 0),
    )
    if not _coding_feedback_entries(state):
        return "fresh implementation: implement the assignment cleanly from the plans."
    retry_limit = max(int(state.get("max_retries", DEFAULT_MAX_RETRIES) or 0), 1)
    if attempts >= max(retry_limit - 1, 1):
        return (
            "root-cause repair: this is a late retry. Re-derive the failing cycle and update all "
            "dependent control/datapath assignments needed to satisfy each acceptance condition, "
            "while preserving unrelated behavior."
        )
    if attempts >= 1:
        return (
            "evidence-driven repair: make the smallest complete functional edit for each active "
            "finding and update directly dependent logic consistently."
        )
    return (
        "targeted repair: make the smallest complete functional code change that directly closes "
        "each reviewer finding."
    )


def _use_review_driven_repair(state: AgentState) -> bool:
    return bool(_coding_feedback_entries(state) and state.get("candidate_files"))


def _render_review_to_code_contract(state: AgentState, max_chars: int) -> str:
    previous_files = state.get("candidate_files", [])
    entries = _coding_feedback_entries(state)
    if not previous_files or not entries:
        return "(none)"

    lines = [
        "Review-to-code repair contract:",
        "- The previous candidate RTL failed review; the next output must be a complete repaired replacement.",
        "- Return every previous candidate file unless a review item explicitly requires deleting or renaming it.",
        "- For each reviewer finding, identify the target RTL behavior and change the corresponding code.",
        f"- Repair intensity: {_coding_repair_intensity(state)}",
        "- Judge closure from each finding's evidence and acceptance condition, not from edit size or a file hash.",
        "- Do not satisfy this contract with explanatory text; only FILE blocks are allowed.",
        "",
        "Previous candidate files that must be repaired or preserved:",
    ]
    for item in build_file_manifest(previous_files):
        sha = str(item.get("sha256", ""))
        lines.append(f"- {item.get('filename')}: {item.get('bytes')} bytes, sha256={sha[:12]}")

    lines.append("")
    lines.append("Blocking review findings to close in code:")
    for idx, entry in enumerate(entries[-8:], start=1):
        report = str(entry.get("report", "")).strip() or "No detailed report."
        tags = ", ".join(_infer_repair_focus_tags(report))
        lines.append(
            f"{idx}. stage={entry.get('stage', 'unknown')} task={entry.get('task_id', 'global')} "
            f"focus={tags}"
        )
        lines.append(f"   code_change_required: {report}")
    return clip_text("\n".join(lines), max_chars)


def _coding_backlog_count(state: AgentState) -> int:
    return len(_coding_feedback_entries(state))


CODING_PROMPT_WEIGHTS = {
    "user_request": 5,
    "architecture_contract": 9,
    "task": 5,
    "supervisor_plan": 10,
    "control_datapath_plan": 10,
    "implementation_obligations": 8,
    "rtl_context": 8,
    "candidate_rtl": 30,
    "previous_candidate_rtl": 24,
    "current_candidate_rtl": 24,
    "invalid_output": 24,
    "coding_repair_backlog": 6,
    "revision_plan": 4,
    "repair_brief": 3,
    "repair_contract": 5,
    "feedback": 1,
    "coding_action_plan": 8,
    "quality_contract": 8,
    "quality_context": 5,
    "gate_report": 8,
    "parser_error": 3,
}


def _budget_coding_prompt_payload(
    payload: dict[str, object], context_limit: int
) -> tuple[dict[str, str], dict[str, object]]:
    def clip_exact(text: str, limit: int) -> str:
        if limit <= 0:
            return ""
        return clip_text(text, limit)

    rendered = {key: str(value) for key, value in payload.items()}
    before_sizes = {key: len(value) for key, value in rendered.items()}
    if context_limit <= 0:
        return rendered, {
            "context_limit": context_limit,
            "budget_disabled": True,
            "total_before": sum(before_sizes.values()),
            "total_after": sum(before_sizes.values()),
            "before_sizes": before_sizes,
            "after_sizes": before_sizes,
        }

    if rendered.get("coding_repair_backlog", "").strip() not in {"", "(none)"}:
        rendered["feedback"] = (
            "(raw feedback consolidated into the coding repair backlog, action plan, "
            "and repair contract above)"
        )
        before_sizes = {key: len(value) for key, value in rendered.items()}

    reserve = min(max(256, context_limit // 10), max(context_limit // 3, 1))
    weighted_budget = max(context_limit - reserve, 1)
    total_before = sum(before_sizes.values())
    if total_before <= weighted_budget:
        return rendered, {
            "context_limit": context_limit,
            "reserved_for_prompt_labels_and_system_message": reserve,
            "weighted_payload_budget": weighted_budget,
            "truncation_required": False,
            "total_before": total_before,
            "total_after": total_before,
            "before_sizes": before_sizes,
            "after_sizes": before_sizes,
        }

    active_weights = {
        key: CODING_PROMPT_WEIGHTS.get(key, 1)
        for key in rendered
    }
    total_weight = sum(active_weights.values()) or 1

    allocations = {
        key: weighted_budget * weight // total_weight
        for key, weight in active_weights.items()
    }
    remaining = weighted_budget - sum(allocations.values())
    remainder_order = sorted(
        active_weights,
        key=lambda key: (weighted_budget * active_weights[key]) % total_weight,
        reverse=True,
    )
    for key in remainder_order[:remaining]:
        allocations[key] += 1

    # Reuse allocations left by short fields so large RTL/code fields receive the
    # full available context instead of being truncated while budget sits idle.
    effective_allocations = {
        key: min(before_sizes[key], allocations[key]) for key in active_weights
    }
    unused = weighted_budget - sum(effective_allocations.values())
    while unused > 0:
        needy = [
            key
            for key in active_weights
            if effective_allocations[key] < before_sizes[key]
        ]
        if not needy:
            break
        needy_weight = sum(active_weights[key] for key in needy) or len(needy)
        granted = 0
        for key in needy:
            need = before_sizes[key] - effective_allocations[key]
            share = max(1, unused * active_weights[key] // needy_weight)
            grant = min(need, share, unused - granted)
            if grant <= 0:
                continue
            effective_allocations[key] += grant
            granted += grant
            if granted >= unused:
                break
        if granted <= 0:
            break
        unused -= granted
    allocations = effective_allocations

    for key in active_weights:
        rendered[key] = clip_exact(rendered[key], allocations[key])

    after_sizes = {key: len(value) for key, value in rendered.items()}
    return rendered, {
        "context_limit": context_limit,
        "reserved_for_prompt_labels_and_system_message": reserve,
        "weighted_payload_budget": weighted_budget,
        "truncation_required": True,
        "unused_payload_budget": weighted_budget - sum(after_sizes.values()),
        "unweighted_keys": sorted(key for key in rendered if key not in CODING_PROMPT_WEIGHTS),
        "total_before": sum(before_sizes.values()),
        "total_after": sum(after_sizes.values()),
        "allocations": allocations,
        "before_sizes": before_sizes,
        "after_sizes": after_sizes,
    }


def _render_deterministic_coding_action_plan(state: AgentState, max_chars: int) -> str:
    task = current_manager_task(state)
    entries = _coding_feedback_entries(state)
    summary_limit = max(300, max_chars // 8)
    lines = [
        "Mandatory RTL coding action plan:",
        "- Translate each obligation below into an observable port, register, state transition, control assignment, datapath operation, or acceptance check.",
        "- Resolve interface, cycle timing, reset, state/control, datapath, width/sign, protocol, and boundary behavior before writing output.",
        "- Produce complete synthesizable Verilog-2001 FILE blocks only after checking every plan item.",
        "",
        f"Task: {task.get('id', 'task')} - {task.get('title', '')}",
        "",
        "Obligation digest:",
        f"- Architecture: {clip_text(state.get('architecture_contract') or '(none)', summary_limit)}",
        f"- Supervisor: {clip_text(state.get('supervisor_plan') or '(none)', summary_limit)}",
        f"- Control/Data Path: {clip_text(state.get('control_datapath_plan') or '(none)', summary_limit)}",
        "",
        "Required implementation decisions:",
        "- Interface and timing: preserve required ports/parameters and define transaction acceptance, latency, completion, and back-to-back behavior.",
        "- Control logic: define current/next state, legal transitions, priority, enables, load/clear, busy, valid/ready, done, and error behavior as applicable.",
        "- Datapath: define every storage element, source mux, arithmetic/comparison operation, update condition, output source, and exact width/sign behavior.",
        "- Reset and boundaries: define every register reset value, recovery to idle, terminal-count behavior, simultaneous controls, and required overflow/underflow policy.",
        "- Acceptance: mentally trace reset, one normal transaction, boundary transactions, stalls, and consecutive transactions before returning code.",
    ]
    if state.get("candidate_files"):
        lines.extend(["", "Previous candidate handling:"])
        lines.extend(
            [
                "- Start from the complete previous candidate RTL.",
                "- Preserve required module interfaces and filenames.",
                "- Repair the behavior named by active acceptance conditions; edit size is not a pass criterion.",
            ]
        )
    if entries:
        lines.extend(["", "Required review repairs:"])
        lines.extend(
            [
                f"- Backlog item count: {_coding_backlog_count(state)}",
                "- Close old unresolved items and new findings in the same RTL revision.",
                "- If multiple findings touch related behavior, rework the shared control/datapath path instead of patching one symptom.",
            ]
        )
        for idx, entry in enumerate(entries[-10:], start=1):
            report = str(entry.get("report", "")).strip() or "No detailed report."
            tags = ", ".join(_infer_repair_focus_tags(report))
            lines.append(
                f"{idx}. stage={entry.get('stage', 'unknown')} "
                f"task={entry.get('task_id', 'global')} focus={tags}"
            )
            lines.append(f"   RTL edit required: {clip_text(report, summary_limit)}")
            lines.append(
                "   Acceptance: the returned Verilog must make this exact report obsolete."
            )
    return clip_text("\n".join(lines), max_chars)


def _build_coding_action_plan(
    state: AgentState,
    task: dict,
    task_id: str,
    prompt_payload: dict,
    section_limit: int,
) -> str:
    deterministic_plan = _render_deterministic_coding_action_plan(state, section_limit)
    system_prompt = _load_coding_prompt("verilog_coding_action_plan.md")
    human_template = """
Original user requirement:
{user_request}

Current Manager task:
{task}

Supervisor detailed assignment:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}

RTL implementation quality contract:
{quality_contract}

Current architecture/review implementation obligations:
{implementation_obligations}

Previous candidate RTL:
{previous_candidate_rtl}

Cumulative coding repair backlog:
{coding_repair_backlog}

Reviewer fix checklist:
{revision_plan}

Targeted repair brief:
{repair_brief}

Review-to-code repair contract:
{repair_contract}

Deterministic minimum action plan:
{deterministic_plan}
"""
    planner_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            (
                "human",
                human_template,
            ),
        ]
    )
    try:
        payload = {
            "user_request": prompt_payload["user_request"],
            "task": render_manager_task(task),
            "supervisor_plan": prompt_payload["supervisor_plan"],
            "control_datapath_plan": prompt_payload["control_datapath_plan"],
            "quality_contract": prompt_payload["quality_contract"],
            "implementation_obligations": prompt_payload["implementation_obligations"],
            "previous_candidate_rtl": clip_text(
                prompt_payload["previous_candidate_rtl"], section_limit
            ),
            "coding_repair_backlog": prompt_payload["coding_repair_backlog"],
            "revision_plan": prompt_payload["revision_plan"],
            "repair_brief": prompt_payload["repair_brief"],
            "repair_contract": prompt_payload["repair_contract"],
            "deterministic_plan": deterministic_plan,
        }
        payload, budget_report = _budget_coding_prompt_payload(
            payload, state.get("max_context_chars", 120_000)
        )
        write_json_artifact(
            f"logs/{task_id}_coding_action_plan_prompt_sizes_attempt_{state.get('coding_retry_count', 0) + 1}.json",
            budget_report,
        )
        log_agent_prompt(
            "verilog_coding_action_plan",
            f"{task_id}_{state.get('coding_retry_count', 0) + 1}",
            system_prompt,
            human_template,
            payload,
        )
        response = (planner_prompt | llm).invoke(payload)
        planner_plan = str(response.content or "").strip()
    except Exception as exc:
        planner_plan = ""
        planner_error = str(exc)
    else:
        planner_error = ""

    if len(planner_plan) < 160:
        reason = (
            f"Action planner unavailable ({planner_error})."
            if planner_error
            else "Action planner returned too little detail."
        )
        plan = f"{deterministic_plan}\n\n{reason} Deterministic plan is authoritative."
    else:
        planner_limit = max(400, section_limit * 2 // 3)
        guardrail_limit = max(300, section_limit - planner_limit)
        plan = (
            "Planner-derived RTL edit plan:\n"
            + clip_text(planner_plan, planner_limit)
            + "\n\nDeterministic implementation guardrails:\n"
            + clip_text(deterministic_plan, guardrail_limit)
        )

    plan = clip_text(plan, section_limit)
    write_text_artifact(
        f"logs/{task_id}_coding_action_plan_attempt_{state.get('coding_retry_count', 0) + 1}.md",
        plan,
    )
    return plan


def _review_repair_delta_report(state: AgentState, files: list[dict[str, str]]) -> str:
    return ""


def _incomplete_review_repair_report(state: AgentState, files: list[dict[str, str]]) -> str:
    if not _use_review_driven_repair(state):
        return ""

    previous_files = state.get("candidate_files", [])
    previous_names = {str(file_info.get("filename", "")).strip() for file_info in previous_files}
    current_names = {str(file_info.get("filename", "")).strip() for file_info in files}
    missing = sorted(name for name in previous_names if name and name not in current_names)
    if missing:
        return (
            "Review-driven repair did not return every previous candidate file. "
            f"Missing repaired/preserved files: {', '.join(missing)}"
        )

    return ""


def _reject_incomplete_review_repair(
    state: AgentState,
    task_id: str,
    files: list[dict[str, str]],
    messages: list,
):
    report = _incomplete_review_repair_report(state, files)
    if not report:
        return None
    attempt = state.get("coding_retry_count", 0) + 1
    print("---VERILOG CODING TEAM: REVIEW REPAIR CONTRACT NOT SATISFIED---")
    write_text_artifact(
        f"failed_attempts/{task_id}_incomplete_review_repair_attempt_{attempt}.txt",
        render_files(files) + "\n\n" + report,
    )
    force_forward = bool(
        state.get("max_retries", DEFAULT_MAX_RETRIES)
        and attempt >= state.get("max_retries", DEFAULT_MAX_RETRIES)
    )
    if force_forward:
        print("---VERILOG CODING TEAM: LOCAL REVIEW FORCE-FORWARD THRESHOLD REACHED---")
    return {
        "candidate_files": files,
        "generation_ok": False,
        "coding_review_forced_forward": force_forward,
        "microarchitecture_passed": False,
        "verification_passed": False,
        "verification_report": report,
        "coding_retry_count": attempt,
        "failed_stage": "" if force_forward else "coding_repair_contract",
        "blocking_report": "" if force_forward else report,
        "messages": messages,
        "error_message": report,
        "review_feedback_log": append_review_feedback(
            state, "coding_gate_internal", report, task_id
        ),
    }


def _coding_preflight_report(state: AgentState, files: list[dict[str, str]]) -> str:
    static_result = static_microarchitecture_review(files)
    merged_files = merge_files(state.get("final_files", []), files)
    lint_result = run_syntax_lint(
        merged_files,
        False,
        state.get("lint_timeout_seconds", 30),
    )
    issues = []
    if not static_result.get("passed"):
        issues.append(f"Static coding preflight failed:\n{static_result.get('report', '')}")
    if not lint_result.get("passed"):
        issues.append(f"Syntax lint preflight failed:\n{lint_result.get('report', '')}")
    if not issues:
        return ""
    return "\n\n".join(issues)


def _hard_review_gate_report(state: AgentState, files: list[dict[str, str]]) -> str:
    reports = []
    for report in (
        _incomplete_review_repair_report(state, files),
        _coding_preflight_report(state, files),
    ):
        if report:
            reports.append(report)
    return "\n\n".join(reports)


def _soft_review_scope_report(state: AgentState, files: list[dict[str, str]]) -> str:
    return _review_repair_delta_report(state, files)


def _review_gate_report(state: AgentState, files: list[dict[str, str]]) -> str:
    reports = []
    for report in (
        _hard_review_gate_report(state, files),
        _soft_review_scope_report(state, files),
    ):
        if report:
            reports.append(report)
    return "\n\n".join(reports)


def _coding_closure_audit(
    state: AgentState,
    task: dict,
    task_id: str,
    files: list[dict[str, str]],
    prompt_payload: dict,
    section_limit: int,
    phase: str,
) -> tuple[bool, str]:
    if not _coding_feedback_entries(state):
        return True, ""

    attempt = state.get("coding_retry_count", 0) + 1
    system_prompt = load_reviewer_prompt("verilog_coding_closure_review.md")
    human_template = """
Original user requirement:
{user_request}

Current Manager task:
{task}

Architecture contract:
{architecture_contract}

Supervisor detailed assignment:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}

Mandatory RTL coding action plan:
{coding_action_plan}

Coding repair backlog whose closure must be checked:
{coding_repair_backlog}

Current RTL candidate:
{candidate_rtl}
"""
    candidate_limit = max(section_limit, state.get("max_context_chars", 120_000) // 2)
    payload = {
        "user_request": prompt_payload["user_request"],
        "task": render_manager_task(task),
        "architecture_contract": prompt_payload["architecture_contract"],
        "supervisor_plan": prompt_payload["supervisor_plan"],
        "control_datapath_plan": prompt_payload["control_datapath_plan"],
        "coding_action_plan": prompt_payload["coding_action_plan"],
        "coding_repair_backlog": prompt_payload["coding_repair_backlog"],
        "candidate_rtl": render_files_for_prompt(files, candidate_limit),
    }
    payload, budget_report = _budget_coding_prompt_payload(
        payload, state.get("max_context_chars", 120_000)
    )
    write_json_artifact(
        f"logs/{task_id}_coding_closure_audit_prompt_sizes_{phase}_attempt_{attempt}.json",
        budget_report,
    )
    log_agent_prompt(
        "verilog_coding_closure_review",
        f"{task_id}_{attempt}_{phase}",
        system_prompt,
        human_template,
        payload,
    )
    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", human_template)]
    )
    try:
        response = (prompt | llm).invoke(payload)
    except Exception as exc:
        write_text_artifact(
            f"logs/{task_id}_coding_closure_audit_{phase}_attempt_{attempt}.md",
            f"Coding closure audit skipped after runtime error: {exc}",
        )
        return True, ""

    passed, report, decision_details = parse_review_result_with_details(
        response.content,
        "Coding closure audit output was not valid JSON.",
    )
    write_text_artifact(
        f"logs/{task_id}_coding_closure_audit_raw_{phase}_attempt_{attempt}.txt",
        response.content,
    )
    write_json_artifact(
        f"logs/{task_id}_coding_closure_audit_decision_{phase}_attempt_{attempt}.json",
        decision_details,
    )

    if (
        decision_details.get("source") == "text"
        and decision_details.get("fallback_text_verdict") is None
    ):
        write_text_artifact(
            f"logs/{task_id}_coding_closure_audit_{phase}_attempt_{attempt}.md",
            "Coding closure audit skipped because the auditor returned no parseable verdict.\n\n"
            + str(response.content),
        )
        return True, ""

    final_report = report or ("PASS" if passed else "Coding repair backlog remains open.")
    write_text_artifact(
        f"logs/{task_id}_coding_closure_audit_{phase}_attempt_{attempt}.md",
        final_report,
    )
    if passed:
        return True, ""
    return False, "Coding closure audit failed:\n" + final_report


def _coding_quality_audit(
    state: AgentState,
    task: dict,
    task_id: str,
    files: list[dict[str, str]],
    prompt_payload: dict,
    section_limit: int,
    phase: str,
) -> tuple[bool, str]:
    attempt = state.get("coding_retry_count", 0) + 1
    static_result = static_microarchitecture_review(files)
    lint_result = run_syntax_lint(
        merge_files(state.get("final_files", []), files),
        False,
        state.get("lint_timeout_seconds", 30),
    )
    quality_context = (
        f"Static microarchitecture result:\n{static_result.get('report', '')}\n\n"
        f"Syntax lint result:\n{lint_result.get('report', '')}"
    )
    system_prompt = load_reviewer_prompt("verilog_coding_quality_review.md")
    human_template = """
Original user requirement:
{user_request}

Current Manager task:
{task}

Architecture contract:
{architecture_contract}

Supervisor detailed assignment:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}

RTL implementation quality contract:
{quality_contract}

Mandatory RTL coding action plan:
{coding_action_plan}

Current architecture/review implementation obligations:
{implementation_obligations}

Static and lint context:
{quality_context}

RTL candidate to audit:
{candidate_rtl}
"""
    candidate_limit = max(section_limit, state.get("max_context_chars", 120_000) // 2)
    payload = {
        "user_request": prompt_payload["user_request"],
        "task": render_manager_task(task),
        "architecture_contract": prompt_payload["architecture_contract"],
        "supervisor_plan": prompt_payload["supervisor_plan"],
        "control_datapath_plan": prompt_payload["control_datapath_plan"],
        "quality_contract": prompt_payload["quality_contract"],
        "coding_action_plan": prompt_payload["coding_action_plan"],
        "implementation_obligations": prompt_payload["implementation_obligations"],
        "quality_context": quality_context,
        "candidate_rtl": render_files_for_prompt(files, candidate_limit),
    }
    payload, budget_report = _budget_coding_prompt_payload(
        payload, state.get("max_context_chars", 120_000)
    )
    write_json_artifact(
        f"logs/{task_id}_coding_quality_audit_prompt_sizes_{phase}_attempt_{attempt}.json",
        budget_report,
    )
    log_agent_prompt(
        "verilog_coding_quality_review",
        f"{task_id}_{attempt}_{phase}",
        system_prompt,
        human_template,
        payload,
    )
    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", human_template)]
    )
    try:
        response = (prompt | llm).invoke(payload)
    except Exception as exc:
        write_text_artifact(
            f"logs/{task_id}_coding_quality_audit_{phase}_attempt_{attempt}.md",
            f"Coding quality audit skipped after runtime error: {exc}",
        )
        return True, ""

    passed, report, decision_details = parse_review_result_with_details(
        response.content,
        "Coding quality audit output was not valid JSON.",
    )
    write_text_artifact(
        f"logs/{task_id}_coding_quality_audit_raw_{phase}_attempt_{attempt}.txt",
        response.content,
    )
    write_json_artifact(
        f"logs/{task_id}_coding_quality_audit_decision_{phase}_attempt_{attempt}.json",
        decision_details,
    )
    if (
        decision_details.get("source") == "text"
        and decision_details.get("fallback_text_verdict") is None
    ):
        write_text_artifact(
            f"logs/{task_id}_coding_quality_audit_{phase}_attempt_{attempt}.md",
            "Coding quality audit skipped because the auditor returned no parseable verdict.\n\n"
            + str(response.content),
        )
        return True, ""

    final_report = report or ("PASS" if passed else "RTL has an objective implementation defect.")
    write_text_artifact(
        f"logs/{task_id}_coding_quality_audit_{phase}_attempt_{attempt}.md",
        final_report,
    )
    if passed:
        return True, ""
    return False, "Coding quality audit failed:\n" + final_report


def _coding_repair_scope_audit(state: AgentState, files: list[dict[str, str]]) -> dict:
    previous_files = state.get("candidate_files", [])
    previous_names = {
        str(file_info.get("filename", "")).strip() for file_info in previous_files
    }
    current_names = {str(file_info.get("filename", "")).strip() for file_info in files}
    return {
        "backlog_count": _coding_backlog_count(state),
        "repair_intensity": _coding_repair_intensity(state),
        "previous_file_count": len(previous_names),
        "current_file_count": len(current_names),
        "new_files": sorted(current_names - previous_names),
        "removed_files": sorted(previous_names - current_names),
        "assessment_basis": "review finding evidence and acceptance conditions",
    }


def _reject_review_gate_failure(
    state: AgentState,
    task_id: str,
    files: list[dict[str, str]],
    messages: list,
    report: str,
):
    attempt = state.get("coding_retry_count", 0) + 1
    print("---VERILOG CODING TEAM: REVIEW GATE NOT SATISFIED---")
    write_json_artifact(
        f"logs/{task_id}_coding_repair_scope_audit_attempt_{attempt}.json",
        _coding_repair_scope_audit(state, files),
    )
    write_text_artifact(
        f"failed_attempts/{task_id}_review_gate_failed_attempt_{attempt}.txt",
        render_files(files) + "\n\n" + report,
    )
    force_forward = bool(
        state.get("max_retries", DEFAULT_MAX_RETRIES)
        and attempt >= state.get("max_retries", DEFAULT_MAX_RETRIES)
    )
    if force_forward:
        print("---VERILOG CODING TEAM: LOCAL REVIEW FORCE-FORWARD THRESHOLD REACHED---")
    return {
        "candidate_files": files,
        "generation_ok": False,
        "coding_review_forced_forward": force_forward,
        "microarchitecture_passed": False,
        "verification_passed": False,
        "verification_report": report,
        "coding_retry_count": attempt,
        "failed_stage": "" if force_forward else "coding_review_gate",
        "blocking_report": "" if force_forward else report,
        "messages": messages,
        "error_message": report,
        "review_feedback_log": append_review_feedback(
            state, "coding_gate_internal", report, task_id
        ),
    }


def _attempt_review_gate_repair(
    state: AgentState,
    task: dict,
    task_id: str,
    files: list[dict[str, str]],
    gate_report: str,
    prompt_payload: dict,
    section_limit: int,
):
    attempt = state.get("coding_retry_count", 0) + 1
    candidate_limit = max(
        section_limit,
        state.get("max_context_chars", 120_000) // 3,
    )
    coding_action_plan = prompt_payload["coding_action_plan"]
    print("---VERILOG CODING TEAM: Local review gate failed; invoking focused repair pass---")
    write_text_artifact(
        f"logs/{task_id}_review_gate_failure_attempt_{attempt}.md",
        gate_report,
    )
    system_prompt = _load_coding_prompt("verilog_review_gate_repair.md")
    human_template = """
Original user requirement:
{user_request}

Architecture contract:
{architecture_contract}

Current Manager task:
{task}

Supervisor detailed assignment:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}

RTL implementation quality contract:
{quality_contract}

Current architecture/review implementation obligations:
{implementation_obligations}

Coding repair intensity:
{repair_intensity}

Mandatory RTL coding action plan:
{coding_action_plan}

Previous candidate RTL that failed earlier review:
{previous_candidate_rtl}

Current candidate RTL rejected by local review gate:
{current_candidate_rtl}

Cumulative coding repair backlog:
{coding_repair_backlog}

Reviewer fix checklist:
{revision_plan}

Targeted repair brief:
{repair_brief}

Review-to-code repair contract:
{repair_contract}

Local review-gate failure that must be closed:
{gate_report}

Raw reviewer feedback:
{feedback}
"""
    repair_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            (
                "human",
                human_template,
            ),
        ]
    )
    payload = {
        "user_request": prompt_payload["user_request"],
        "architecture_contract": prompt_payload["architecture_contract"],
        "task": render_manager_task(task),
        "supervisor_plan": prompt_payload["supervisor_plan"],
        "control_datapath_plan": prompt_payload["control_datapath_plan"],
        "quality_contract": prompt_payload["quality_contract"],
        "implementation_obligations": prompt_payload["implementation_obligations"],
        "repair_intensity": prompt_payload["repair_intensity"],
        "coding_action_plan": coding_action_plan,
        "previous_candidate_rtl": prompt_payload["previous_candidate_rtl"],
        "current_candidate_rtl": render_files_for_prompt(files, candidate_limit),
        "coding_repair_backlog": prompt_payload["coding_repair_backlog"],
        "revision_plan": prompt_payload["revision_plan"],
        "repair_brief": prompt_payload["repair_brief"],
        "repair_contract": prompt_payload["repair_contract"],
        "gate_report": gate_report,
        "feedback": prompt_payload["feedback"],
    }
    payload, budget_report = _budget_coding_prompt_payload(
        payload, state.get("max_context_chars", 120_000)
    )
    write_json_artifact(
        f"logs/{task_id}_review_gate_repair_prompt_sizes_attempt_{attempt}.json",
        budget_report,
    )
    log_agent_prompt(
        "verilog_review_gate_repair",
        f"{task_id}_{attempt}",
        system_prompt,
        human_template,
        payload,
    )
    repair_response = (repair_prompt | llm).invoke(payload)
    write_text_artifact(
        f"logs/{task_id}_review_gate_repair_raw_attempt_{attempt}.txt",
        repair_response.content,
    )
    try:
        repaired_files = parse_generated_files_response(repair_response.content)
        is_valid, validation_error = validate_coding_candidate_files(
            repaired_files,
            state.get("max_generated_file_bytes", 500_000),
            state.get("max_generated_files", 64),
        )
        if not is_valid:
            raise ValueError(validation_error)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        write_text_artifact(
            f"failed_attempts/{task_id}_review_gate_repair_invalid_attempt_{attempt}.txt",
            repair_response.content,
        )
        return (
            None,
            _code_free_status_message(
                "review-gate repair",
                task_id,
                f"invalid output rejected; raw response saved to failed_attempts/{task_id}_review_gate_repair_invalid_attempt_{attempt}.txt",
            ),
            str(exc),
        )

    return (
        repaired_files,
        _code_free_status_message(
            "review-gate repair",
            task_id,
            f"parsed {len(repaired_files)} revised file(s); raw response saved under logs/",
        ),
        "",
    )


def _repair_candidate_against_review_gate(
    state: AgentState,
    task: dict,
    task_id: str,
    files: list[dict[str, str]],
    prompt_payload: dict,
    section_limit: int,
    messages: list,
):
    initial_hard_report = _hard_review_gate_report(state, files)
    gate_report = _review_gate_report(state, files)
    if not initial_hard_report:
        _closure_passed, closure_report = _coding_closure_audit(
            state,
            task,
            task_id,
            files,
            prompt_payload,
            section_limit,
            "initial",
        )
        if closure_report:
            gate_report = "\n\n".join(item for item in (gate_report, closure_report) if item)
        _quality_passed, quality_report = _coding_quality_audit(
            state,
            task,
            task_id,
            files,
            prompt_payload,
            section_limit,
            "initial",
        )
        if quality_report:
            gate_report = "\n\n".join(item for item in (gate_report, quality_report) if item)
    if not gate_report:
        return files, messages, ""

    repaired_files, repair_response, repair_error = _attempt_review_gate_repair(
        state, task, task_id, files, gate_report, prompt_payload, section_limit
    )
    next_messages = messages + ([repair_response] if repair_response else [])
    if repaired_files is None:
        if not initial_hard_report:
            write_text_artifact(
                f"logs/{task_id}_review_gate_soft_warning_attempt_{state.get('coding_retry_count', 0) + 1}.md",
                gate_report
                + f"\n\nAutomated repair failed: {repair_error}\nReturning to Coding Team for an evidence-driven revision.",
            )
            return (
                files,
                next_messages,
                gate_report
                + "\n\nAutomated repair failed. The next coding retry must directly satisfy the reported acceptance conditions.",
            )
        return (
            files,
            next_messages,
            gate_report + f"\n\nAutomated review-gate repair failed: {repair_error}",
        )

    second_hard_report = _hard_review_gate_report(state, repaired_files)
    second_soft_report = _soft_review_scope_report(state, repaired_files)
    if second_hard_report:
        return (
            repaired_files,
            next_messages,
            second_hard_report
            + "\n\nAutomated review-gate repair was attempted but did not close hard gate issues.",
        )
    if second_soft_report:
        write_text_artifact(
            f"logs/{task_id}_review_gate_soft_warning_attempt_{state.get('coding_retry_count', 0) + 1}.md",
            second_soft_report
            + "\n\nAutomated review-gate repair did not satisfy the active acceptance conditions. Returning to Coding Team.",
        )
        write_json_artifact(
            f"logs/{task_id}_review_gate_soft_scope_audit_attempt_{state.get('coding_retry_count', 0) + 1}.json",
            _coding_repair_scope_audit(state, repaired_files),
        )
        return (
            repaired_files,
            next_messages,
            second_soft_report
            + "\n\nAutomated review-gate repair was attempted but did not make a broad enough functional RTL change.",
        )

    post_repair_reports = []
    _closure_passed, second_closure_report = _coding_closure_audit(
        state,
        task,
        task_id,
        repaired_files,
        prompt_payload,
        section_limit,
        "repaired",
    )
    if second_closure_report:
        post_repair_reports.append(second_closure_report)
    _quality_passed, second_quality_report = _coding_quality_audit(
        state,
        task,
        task_id,
        repaired_files,
        prompt_payload,
        section_limit,
        "repaired",
    )
    if second_quality_report:
        post_repair_reports.append(second_quality_report)
    if post_repair_reports:
        return (
            repaired_files,
            next_messages,
            "\n\n".join(post_repair_reports)
            + "\n\nAutomated review-gate repair was attempted but objective RTL issues remain.",
        )

    write_json_artifact(
        f"logs/{task_id}_review_gate_repaired_attempt_{state.get('coding_retry_count', 0) + 1}.json",
        repaired_files,
    )
    return repaired_files, next_messages, ""


def _reject_failed_preflight(
    state: AgentState,
    task_id: str,
    files: list[dict[str, str]],
    messages: list,
):
    report = _coding_preflight_report(state, files)
    if not report:
        return None
    attempt = state.get("coding_retry_count", 0) + 1
    print("---VERILOG CODING TEAM: PREFLIGHT FAIL; RETRYING CODING---")
    write_text_artifact(
        f"failed_attempts/{task_id}_coding_preflight_failed_attempt_{attempt}.txt",
        render_files(files) + "\n\n" + report,
    )
    force_forward = bool(
        state.get("max_retries", DEFAULT_MAX_RETRIES)
        and attempt >= state.get("max_retries", DEFAULT_MAX_RETRIES)
    )
    if force_forward:
        print("---VERILOG CODING TEAM: LOCAL REVIEW FORCE-FORWARD THRESHOLD REACHED---")
    return {
        "candidate_files": files,
        "generation_ok": False,
        "coding_review_forced_forward": force_forward,
        "microarchitecture_passed": False,
        "verification_passed": False,
        "verification_report": report,
        "coding_retry_count": attempt,
        "failed_stage": "" if force_forward else "coding_preflight",
        "blocking_report": "" if force_forward else report,
        "messages": messages,
        "error_message": report,
        "review_feedback_log": append_review_feedback(
            state, "coding_preflight", report, task_id
        ),
    }


def _render_microarchitecture_repair_packet(
    state: AgentState,
    task_id: str,
    report: str,
    static_report: str,
) -> str:
    task = current_manager_task(state)
    lines = [
        "Microarchitecture-to-coding repair packet:",
        f"- task: {task.get('id', task_id)} - {task.get('title', '')}",
        "- verdict: FAIL",
        "- Coding Team must fix the still-observable microarchitecture findings below.",
        "- Update directly dependent control and datapath logic consistently while preserving unrelated behavior.",
        "",
        "Static microarchitecture scan context:",
        str(static_report or "(none)").strip(),
        "",
        "New blocking microarchitecture finding:",
        str(report or "Microarchitecture review failed.").strip(),
        "",
        "Required coding response:",
        "- Modify FSM/current-state/next-state logic, control enables, done/valid/ready, load/clear, reset paths, and datapath registers as a coordinated edit when relevant.",
        "- Return complete Verilog-2001 files for every reviewed candidate file.",
        "- Ensure the next microarchitecture reviewer cannot repeat the same prior or new finding.",
    ]
    return "\n".join(lines)


@_with_runtime
def verilog_coding_team_agent(state: AgentState):
    task = current_manager_task(state)
    task_id = sanitize_artifact_name(task.get("id"), "task")
    task_label = str(task.get("id") or task_id)
    print(f"---VERILOG CODING TEAM: Implementing {task_label}---")
    context_limit = state.get("max_context_chars", 120_000)
    section_limit = split_context_budget(context_limit, 10)
    candidate_source_limit = max(section_limit, context_limit // 3)
    feedback = _review_feedback_for_coding(state, section_limit)
    revision_plan = _render_coding_revision_plan(state, section_limit)
    repair_brief = _render_targeted_repair_brief(state, section_limit)
    repair_contract = _render_review_to_code_contract(state, section_limit)
    repair_intensity = _coding_repair_intensity(state)
    review_driven_repair = _use_review_driven_repair(state)
    if feedback != "(none)":
        feedback = (
            "\nRaw reviewer and format feedback:\n"
            f"{feedback}"
        )

    if review_driven_repair:
        system_prompt = _load_coding_prompt("verilog_implementation_repair.md")
        human_prompt = """
Original user requirement:
{user_request}

Architecture contract:
{architecture_contract}

Current Manager task:
{task}

Supervisor detailed assignment:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}

RTL implementation quality contract:
{quality_contract}

Current architecture/review implementation obligations:
{implementation_obligations}

Mandatory RTL coding action plan:
{coding_action_plan}

Coding revision mode:
{revision_mode}

Coding repair intensity:
{repair_intensity}

Accepted RTL files from earlier tasks:
{rtl_context}

Previous candidate manifest:
{previous_candidate_manifest}

Previous candidate RTL that failed review:
{previous_candidate_rtl}

Cumulative coding repair backlog:
{coding_repair_backlog}

Reviewer fix checklist:
{revision_plan}

Targeted repair brief:
{repair_brief}

Review-to-code repair contract:
{repair_contract}
{feedback}
"""
    else:
        system_prompt = _load_coding_prompt("verilog_coding.md")
        human_prompt = """
Original user requirement:
{user_request}

Architecture contract:
{architecture_contract}

Current Manager task:
{task}

Supervisor detailed assignment:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}

RTL implementation quality contract:
{quality_contract}

Current architecture/review implementation obligations:
{implementation_obligations}

Mandatory RTL coding action plan:
{coding_action_plan}

Coding revision mode:
{revision_mode}

Coding repair intensity:
{repair_intensity}

Current RTL files:
{rtl_context}

Previous candidate manifest:
{previous_candidate_manifest}

Previous candidate RTL to revise, if any:
{previous_candidate_rtl}

Cumulative coding repair backlog:
{coding_repair_backlog}

Reviewer fix checklist:
{revision_plan}

Targeted repair brief:
{repair_brief}

Review-to-code repair contract:
{repair_contract}
{feedback}
"""
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            ("human", human_prompt),
        ]
    )
    prompt_payload = {
        "user_request": clip_text(state["user_request"], section_limit),
        "architecture_contract": clip_text(
            state.get("architecture_contract") or "(none)", section_limit
        ),
        "task": render_manager_task(task),
        "supervisor_plan": clip_text(state.get("supervisor_plan") or "(none)", section_limit),
        "control_datapath_plan": clip_text(
            state.get("control_datapath_plan") or "(none)", section_limit
        ),
        "quality_contract": _render_rtl_quality_contract(state, task, section_limit),
        "implementation_obligations": _render_implementation_obligation_packet(
            state, task, section_limit
        ),
        "rtl_context": clip_text(state.get("rtl_context") or "(none)", section_limit),
        "revision_mode": _coding_revision_mode(state),
        "repair_intensity": repair_intensity,
        "previous_candidate_manifest": _render_previous_candidate_manifest(
            state.get("candidate_files", [])
        ),
        "previous_candidate_rtl": _render_previous_candidate_for_coding(
            state, candidate_source_limit
        ),
        "coding_repair_backlog": render_coding_repair_backlog(state, section_limit),
        "revision_plan": revision_plan,
        "repair_brief": repair_brief,
        "repair_contract": repair_contract,
        "feedback": feedback,
    }
    coding_action_plan = _build_coding_action_plan(
        state, task, task_id, prompt_payload, section_limit
    )
    prompt_payload["coding_action_plan"] = coding_action_plan
    prompt_payload, prompt_budget_report = _budget_coding_prompt_payload(
        prompt_payload, context_limit
    )
    write_text_artifact(
        f"logs/{task_id}_implementation_obligations_attempt_{state.get('coding_retry_count', 0) + 1}.md",
        prompt_payload["implementation_obligations"],
    )
    write_text_artifact(
        f"logs/{task_id}_coding_revision_plan_attempt_{state.get('coding_retry_count', 0) + 1}.md",
        revision_plan,
    )
    write_text_artifact(
        f"logs/{task_id}_coding_repair_brief_attempt_{state.get('coding_retry_count', 0) + 1}.md",
        repair_brief,
    )
    write_text_artifact(
        f"logs/{task_id}_coding_repair_contract_attempt_{state.get('coding_retry_count', 0) + 1}.md",
        repair_contract,
    )
    write_json_artifact(
        f"logs/{task_id}_coding_prompt_sizes_attempt_{state.get('coding_retry_count', 0) + 1}.json",
        dict(
            prompt_budget_report,
            section_limit=section_limit,
            review_driven_repair=review_driven_repair,
        ),
    )
    attempt = state.get("coding_retry_count", 0) + 1
    log_agent_prompt(
        "verilog_coding_team",
        f"{task_id}_{attempt}",
        system_prompt,
        human_prompt,
        prompt_payload,
    )
    response = (prompt | llm).invoke(prompt_payload)
    write_text_artifact(
        f"logs/{task_id}_coding_raw_attempt_{attempt}.txt",
        response.content,
    )
    initial_status_message = _code_free_status_message(
        "coding",
        task_id,
        f"raw response saved to logs/{task_id}_coding_raw_attempt_{attempt}.txt",
    )

    try:
        files = parse_generated_files_response(response.content)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        files = []
        initial_error_kind = "parse"
        initial_error = exc
    else:
        is_valid, validation_error = validate_coding_candidate_files(
            files,
            state.get("max_generated_file_bytes", 500_000),
            state.get("max_generated_files", 64),
        )
        if not is_valid:
            initial_error_kind = "validation"
            initial_error = ValueError(validation_error)
            write_json_artifact(
                f"failed_attempts/{task_id}_parsed_but_invalid_attempt_{state.get('coding_retry_count', 0) + 1}.json",
                files,
            )
            write_text_artifact(
                f"failed_attempts/{task_id}_parsed_but_invalid_attempt_{state.get('coding_retry_count', 0) + 1}.files.txt",
                render_file_blocks(files),
            )
        else:
            initial_error_kind = ""
            initial_error = None

    if initial_error is None:
        files, accepted_messages, gate_report = _repair_candidate_against_review_gate(
            state, task, task_id, files, prompt_payload, section_limit, [initial_status_message]
        )
        if gate_report:
            return _reject_review_gate_failure(
                state, task_id, files, accepted_messages, gate_report
            )
        print(f"---VERILOG CODING TEAM: Generated {len(files)} candidate files.---")
        write_json_artifact(
            f"logs/{task_id}_coding_attempt_{state.get('coding_retry_count', 0) + 1}.json",
            files,
        )
        write_json_artifact(
            f"logs/{task_id}_coding_repair_scope_audit_attempt_{state.get('coding_retry_count', 0) + 1}.json",
            _coding_repair_scope_audit(state, files),
        )
        return {
            "candidate_files": files,
            "generation_ok": True,
            "coding_review_forced_forward": False,
            "microarchitecture_passed": False,
            "messages": accepted_messages,
            "failed_stage": "",
            "blocking_report": "",
            "error_message": "",
        }

    exc = initial_error
    print(f"---WARNING: Coding team output failed {initial_error_kind}, attempting repair: {exc}---")
    try:
        repair_system_prompt = _load_coding_prompt("verilog_coding_repair.md")
        repair_human_template = """
Current Manager task:
{task}

Supervisor detailed assignment:
{supervisor_plan}

Architecture contract:
{architecture_contract}

Control/Data Path plan:
{control_datapath_plan}

Current architecture/review implementation obligations:
{implementation_obligations}

Reviewer fix checklist:
{revision_plan}

Targeted repair brief:
{repair_brief}

Review-to-code repair contract:
{repair_contract}

Coding repair intensity:
{repair_intensity}

Mandatory RTL coding action plan:
{coding_action_plan}

Cumulative coding repair backlog:
{coding_repair_backlog}

Previous candidate RTL, if any:
{previous_candidate_rtl}

Invalid coding output:
{invalid_output}

Parser or validation error:
{parser_error}
"""
        repair_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    repair_system_prompt,
                ),
                (
                    "human",
                    repair_human_template,
                ),
            ]
        )
        repair_payload = {
            "task": render_manager_task(task),
            "supervisor_plan": state.get("supervisor_plan") or "(none)",
            "architecture_contract": prompt_payload["architecture_contract"],
            "control_datapath_plan": prompt_payload["control_datapath_plan"],
            "implementation_obligations": prompt_payload["implementation_obligations"],
            "revision_plan": revision_plan,
            "repair_brief": repair_brief,
            "repair_contract": repair_contract,
            "repair_intensity": repair_intensity,
            "coding_action_plan": coding_action_plan,
            "coding_repair_backlog": prompt_payload["coding_repair_backlog"],
            "previous_candidate_rtl": _render_previous_candidate_for_coding(
                state, candidate_source_limit
            ),
            "invalid_output": response.content,
            "parser_error": str(exc),
        }
        repair_payload, repair_budget_report = _budget_coding_prompt_payload(
            repair_payload, context_limit
        )
        write_json_artifact(
            f"logs/{task_id}_coding_repair_prompt_sizes_attempt_{attempt}.json",
            repair_budget_report,
        )
        log_agent_prompt(
            "verilog_coding_repair",
            f"{task_id}_{attempt}",
            repair_system_prompt,
            repair_human_template,
            repair_payload,
        )
        repair_response = (repair_prompt | llm).invoke(repair_payload)
        write_text_artifact(
            f"logs/{task_id}_coding_repair_raw_attempt_{state.get('coding_retry_count', 0) + 1}.txt",
            repair_response.content,
        )
        try:
            files = parse_generated_files_response(repair_response.content)
            is_valid, validation_error = validate_coding_candidate_files(
                files,
                state.get("max_generated_file_bytes", 500_000),
                state.get("max_generated_files", 64),
            )
            if not is_valid:
                write_json_artifact(
                    f"failed_attempts/{task_id}_repair_parsed_but_invalid_attempt_{state.get('coding_retry_count', 0) + 1}.json",
                    files,
                )
                write_text_artifact(
                    f"failed_attempts/{task_id}_repair_parsed_but_invalid_attempt_{state.get('coding_retry_count', 0) + 1}.files.txt",
                    render_file_blocks(files),
                )
                raise ValueError(validation_error)
            files, accepted_messages, gate_report = _repair_candidate_against_review_gate(
                state,
                task,
                task_id,
                files,
                prompt_payload,
                section_limit,
                [
                    initial_status_message,
                    _code_free_status_message(
                        "coding format repair",
                        task_id,
                        f"raw response saved to logs/{task_id}_coding_repair_raw_attempt_{attempt}.txt",
                    ),
                ],
            )
            if gate_report:
                return _reject_review_gate_failure(
                    state, task_id, files, accepted_messages, gate_report
                )
            print(f"---VERILOG CODING TEAM: Repaired {len(files)} candidate files.---")
            write_json_artifact(
                f"logs/{task_id}_coding_attempt_{state.get('coding_retry_count', 0) + 1}.json",
                files,
            )
            write_json_artifact(
                f"logs/{task_id}_coding_repair_scope_audit_attempt_{state.get('coding_retry_count', 0) + 1}.json",
                _coding_repair_scope_audit(state, files),
            )
            return {
                "candidate_files": files,
                "generation_ok": True,
                "coding_review_forced_forward": False,
                "microarchitecture_passed": False,
                "messages": accepted_messages,
                "failed_stage": "",
                "blocking_report": "",
                "error_message": f"Coding output repaired after invalid format: {exc}",
            }
        except (json.JSONDecodeError, TypeError, ValueError) as repair_exc:
            exc = repair_exc
            write_text_artifact(
                f"failed_attempts/{task_id}_repair_failed_attempt_{state.get('coding_retry_count', 0) + 1}.txt",
                repair_response.content,
            )
    except Exception as repair_runtime_exc:
        exc = repair_runtime_exc

    print(f"---ERROR: Coding team produced invalid file output: {exc}---")
    write_text_artifact(
        f"failed_attempts/{task_id}_invalid_coding_output_attempt_{state.get('coding_retry_count', 0) + 1}.txt",
        response.content,
    )
    report = (
        f"Coding output {initial_error_kind} failed: {exc}. Regenerate using FILE blocks "
        "with one complete synthesizable Verilog-2001 .v/.vh file per block."
    )
    return {
        "generation_ok": False,
        "coding_review_forced_forward": False,
        "microarchitecture_passed": False,
        "verification_passed": False,
        "verification_report": report,
        "coding_retry_count": state.get("coding_retry_count", 0) + 1,
        "failed_stage": "coding",
        "blocking_report": report,
        "messages": [
            _code_free_status_message(
                "coding",
                task_id,
                f"output remained invalid after repair; raw responses saved under failed_attempts/ and logs/",
            )
        ],
        "error_message": str(exc),
        "review_feedback_log": append_review_feedback(state, "coding_format", report, task_id),
    }


@_with_runtime
def microarchitecture_reviewer_agent(state: AgentState):
    task = current_manager_task(state)
    task_id = sanitize_artifact_name(task.get("id"), "task")
    print(f"---MICROARCH REVIEWER: Checking control/datapath implementation for {task['id']}---")
    merged_files = merge_files(state.get("final_files", []), state.get("candidate_files", []))
    static_result = static_microarchitecture_review(state.get("candidate_files", []))
    previous_feedback = render_coding_repair_backlog(
        state, state.get("max_context_chars", 120_000)
    )
    write_text_artifact(
        f"logs/{task_id}_microarchitecture_static_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.txt",
        static_result["report"],
    )
    write_text_artifact(
        f"logs/{task_id}_microarchitecture_prior_backlog_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.md",
        previous_feedback,
    )

    system_prompt = load_reviewer_prompt("microarchitecture_review.md")
    human_template = """
Architecture contract:
{architecture_contract}

Current Manager task:
{task}

Supervisor detailed assignment:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}

Static microarchitecture scan:
{static_report}

Previous coding repair backlog:
{previous_feedback}

RTL candidate:
{candidate_rtl}
"""
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                system_prompt,
            ),
            (
                "human",
                human_template,
            ),
        ]
    )
    payload = {
        "architecture_contract": state.get("architecture_contract") or "(none)",
        "task": render_manager_task(task),
        "supervisor_plan": state["supervisor_plan"],
        "control_datapath_plan": state.get("control_datapath_plan") or "(none)",
        "static_report": static_result["report"],
        "previous_feedback": previous_feedback,
        "candidate_rtl": render_files_for_prompt(
            merged_files, state.get("max_context_chars", 120_000)
        ),
    }
    log_agent_prompt(
        "microarchitecture_review",
        f"{task_id}_{state.get('microarchitecture_retry_count', 0) + 1}",
        system_prompt,
        human_template,
        payload,
    )
    response = (prompt | llm).invoke(payload)

    passed, report, decision_details = parse_review_result_with_details(
        response.content, "Microarchitecture review output was not valid JSON."
    )
    passed = passed and static_result["passed"]

    if not static_result["passed"]:
        report = f"Static microarchitecture scan failed:\n{static_result['report']}\n\n{report}"
    decision_details["static_microarchitecture_passed"] = static_result["passed"]
    decision_details["static_microarchitecture_report"] = static_result["report"]
    decision_details["final_passed_after_static_scan"] = passed
    decision_details["final_report_after_static_scan"] = report

    write_text_artifact(
        f"logs/{task_id}_microarchitecture_review_raw_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.txt",
        response.content,
    )
    write_json_artifact(
        f"logs/{task_id}_microarchitecture_review_decision_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.json",
        decision_details,
    )
    write_text_artifact(
        f"logs/{task_id}_microarchitecture_review_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.md",
        report or ("PASS" if passed else "FAIL"),
    )

    if passed:
        print("---MICROARCH REVIEWER: PASS---")
        return {
            "microarchitecture_passed": True,
            "microarchitecture_review_forced_forward": False,
            "microarchitecture_report": report or "PASS",
            "failed_stage": "",
            "blocking_report": "",
            "messages": [response],
        }

    repair_packet = _render_microarchitecture_repair_packet(
        state,
        task_id,
        report or "Microarchitecture review failed.",
        static_result["report"],
    )
    write_text_artifact(
        f"failed_attempts/{task_id}_microarchitecture_failed_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.txt",
        render_files(state.get("candidate_files", [])) + "\n\n" + repair_packet,
    )
    next_retry_count = state.get("microarchitecture_retry_count", 0) + 1
    force_forward_after = state.get("max_retries", DEFAULT_MAX_RETRIES)
    force_forward = bool(force_forward_after and next_retry_count >= force_forward_after)
    if force_forward:
        print("---MICROARCH REVIEWER: FORCE-FORWARD THRESHOLD REACHED---")
        repair_packet = (
            "FORCED_FORWARD: Microarchitecture review reached the force-forward threshold. "
            "Proceeding to Verification with the best available RTL candidate.\n\n"
            + repair_packet
        )
    else:
        print("---MICROARCH REVIEWER: FAIL---")
    return {
        "microarchitecture_passed": False,
        "microarchitecture_review_forced_forward": force_forward,
        "microarchitecture_report": repair_packet,
        "microarchitecture_retry_count": next_retry_count,
        "failed_stage": "" if force_forward else "microarchitecture_review",
        "blocking_report": "" if force_forward else repair_packet,
        "review_feedback_log": append_review_feedback(
            state,
            "microarchitecture_review",
            report or "Microarchitecture review failed.",
            task_id,
        ),
        "forced_forward_debt": (
            append_forced_forward_debt(
                state,
                "microarchitecture_review",
                report or "Microarchitecture review failed.",
                task_id,
            )
            if force_forward
            else state.get("forced_forward_debt", [])
        ),
        "messages": [response],
    }
