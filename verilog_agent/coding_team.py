from __future__ import annotations

import difflib

from .runtime import refresh_globals


def _with_runtime(fn):
    def wrapped(*args, **kwargs):
        refresh_globals(globals())
        return fn(*args, **kwargs)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


IMPLEMENTATION_REVIEW_STAGES = (
    "control_datapath_review",
    "microarchitecture_review",
    "verification",
    "verification_lint",
    "coding_format",
    "coding_unchanged",
    "coding_preflight",
    "coding_review_gate",
)


def _review_feedback_for_coding(state: AgentState, max_chars: int) -> str:
    return render_review_feedback(state, IMPLEMENTATION_REVIEW_STAGES, max_chars)


def _coding_feedback_entries(state: AgentState) -> list[dict[str, str]]:
    stage_set = set(IMPLEMENTATION_REVIEW_STAGES)
    return [
        entry
        for entry in state.get("review_feedback_log", [])
        if entry.get("stage") in stage_set
    ]


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


def _coding_repair_intensity(state: AgentState) -> str:
    attempts = max(
        state.get("coding_retry_count", 0),
        state.get("microarchitecture_retry_count", 0),
        state.get("verification_retry_count", 0),
    )
    if not _coding_feedback_entries(state):
        return "fresh implementation: implement the assignment cleanly from the plans."
    if attempts >= 10:
        return (
            "full structural repair: repeated review failures mean local patching is not enough. "
            "Rebuild the affected control/datapath implementation from the architecture, "
            "Supervisor assignment, and Control/Data Path plan while preserving required "
            "interfaces and file names."
        )
    if attempts >= 6:
        return (
            "high intensity repair: the same RTL has failed repeatedly. Rework the affected "
            "control/datapath block, reset behavior, handshakes, and state/datapath partitioning "
            "instead of applying a small local tweak."
        )
    if attempts >= 3:
        return (
            "medium intensity repair: make a concrete functional RTL change for each reviewer "
            "finding and update related control/datapath logic consistently."
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
        "- At least one previous candidate file must contain a functional RTL change, not only comments or formatting.",
        "- For each reviewer finding, identify the target RTL behavior and change the corresponding code.",
        f"- Repair intensity: {_coding_repair_intensity(state)}",
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


def _render_deterministic_coding_action_plan(state: AgentState, max_chars: int) -> str:
    task = current_manager_task(state)
    entries = _coding_feedback_entries(state)
    repair_backlog = render_coding_repair_backlog(state, max_chars)
    lines = [
        "Mandatory RTL coding action plan:",
        "- Produce complete Verilog-2001 FILE blocks only.",
        "- Implement the current Manager task and Supervisor assignment directly in RTL.",
        "- Keep control logic and datapath logic explicit and synthesizable.",
        "- Use this plan as a code-edit checklist before returning files.",
        "",
        f"Task: {task.get('id', 'task')} - {task.get('title', '')}",
        "",
        "Cumulative repair backlog digest:",
        repair_backlog,
    ]
    if state.get("candidate_files"):
        lines.extend(
            [
                "",
                "Previous candidate handling:",
                "- Start from the previous candidate RTL.",
                "- Preserve required module interfaces and filenames.",
                "- Make real functional changes in the RTL behavior, not comments or formatting.",
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
            lines.append(f"   RTL edit required: {report}")
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
    if not (_coding_feedback_entries(state) or state.get("candidate_files")):
        write_text_artifact(
            f"logs/{task_id}_coding_action_plan_attempt_{state.get('coding_retry_count', 0) + 1}.md",
            deterministic_plan,
        )
        return deterministic_plan

    planner_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", load_prompt("verilog_coding_action_plan.md")),
            (
                "human",
                """
Original user requirement:
{user_request}

Current Manager task:
{task}

Supervisor detailed assignment:
{supervisor_plan}

Control/Data Path plan:
{control_datapath_plan}

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
""",
            ),
        ]
    )
    try:
        response = (planner_prompt | llm).invoke(
            {
                "user_request": prompt_payload["user_request"],
                "task": render_manager_task(task),
                "supervisor_plan": prompt_payload["supervisor_plan"],
                "control_datapath_plan": prompt_payload["control_datapath_plan"],
                "previous_candidate_rtl": prompt_payload["previous_candidate_rtl"],
                "coding_repair_backlog": prompt_payload["coding_repair_backlog"],
                "revision_plan": prompt_payload["revision_plan"],
                "repair_brief": prompt_payload["repair_brief"],
                "repair_contract": prompt_payload["repair_contract"],
                "deterministic_plan": deterministic_plan,
            }
        )
        plan = str(response.content or "").strip()
    except Exception as exc:
        plan = f"{deterministic_plan}\n\nAction planner failed; use deterministic plan. Error: {exc}"

    if len(plan) < 120:
        plan = (
            deterministic_plan
            + "\n\nAction planner returned too little detail; deterministic plan is authoritative."
        )

    plan = clip_text(plan, section_limit)
    write_text_artifact(
        f"logs/{task_id}_coding_action_plan_attempt_{state.get('coding_retry_count', 0) + 1}.md",
        plan,
    )
    return plan


def _functional_files_fingerprint(files: list[dict[str, str]]) -> str:
    entries = []
    for file_info in sorted(files, key=lambda item: item.get("filename", "")):
        filename = str(file_info.get("filename", "")).strip()
        content = str(file_info.get("content", ""))
        stripped = strip_hdl_comments(content)
        normalized = re.sub(r"\s+", "", stripped)
        entries.append(
            {
                "filename": filename,
                "sha256": hashlib.sha256(normalized.encode()).hexdigest(),
            }
        )
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _functional_file_hashes(files: list[dict[str, str]]) -> dict[str, str]:
    hashes = {}
    for file_info in files:
        filename = str(file_info.get("filename", "")).strip()
        content = str(file_info.get("content", ""))
        stripped = strip_hdl_comments(content)
        normalized = re.sub(r"\s+", "", stripped)
        hashes[filename] = hashlib.sha256(normalized.encode()).hexdigest()
    return hashes


def _functional_compare_text(files: list[dict[str, str]]) -> str:
    chunks = []
    for file_info in sorted(files, key=lambda item: item.get("filename", "")):
        filename = str(file_info.get("filename", "")).strip()
        content = str(file_info.get("content", ""))
        stripped = strip_hdl_comments(content)
        normalized = re.sub(r"\s+", " ", stripped).strip()
        chunks.append(f"FILE {filename}\n{normalized}")
    return "\n".join(chunks)


def _review_repair_delta_report(state: AgentState, files: list[dict[str, str]]) -> str:
    if not _use_review_driven_repair(state):
        return ""

    backlog_count = _coding_backlog_count(state)
    attempts = max(
        state.get("coding_retry_count", 0),
        state.get("microarchitecture_retry_count", 0),
        state.get("verification_retry_count", 0),
    )
    if attempts < 3 and backlog_count < 2:
        return ""

    previous_files = state.get("candidate_files", [])
    previous_text = _functional_compare_text(state.get("candidate_files", []))
    current_text = _functional_compare_text(files)
    if not previous_text or not current_text:
        return ""

    ratio = difflib.SequenceMatcher(
        None, previous_text, current_text, autojunk=False
    ).ratio()
    max_len = max(len(previous_text), len(current_text), 1)
    changed_chars = int(max_len * (1.0 - ratio))
    previous_hashes = _functional_file_hashes(previous_files)
    current_hashes = _functional_file_hashes(files)
    changed_files = sorted(
        name
        for name, previous_hash in previous_hashes.items()
        if current_hashes.get(name) != previous_hash
    )
    previous_file_count = len(previous_hashes)

    if attempts >= 10 or backlog_count >= 5:
        threshold = min(max(160, int(max_len * 0.10)), 1600)
    elif attempts >= 6 or backlog_count >= 3:
        threshold = min(max(120, int(max_len * 0.07)), 1000)
    else:
        threshold = min(max(80, int(max_len * 0.05)), 800)

    scope_issues = []
    if changed_chars < threshold:
        scope_issues.append(
            f"estimated changed functional characters: {changed_chars}; required at least {threshold}"
        )
    if (
        previous_file_count >= 2
        and backlog_count >= 3
        and len(changed_files) < min(2, previous_file_count)
    ):
        scope_issues.append(
            "only one reviewed file changed while multiple backlog items remain; update the related files/modules together"
        )
    if not scope_issues:
        return ""

    return (
        "Review-driven repair scope is too small for the accumulated feedback. "
        + "; ".join(scope_issues)
        + ". Rework the affected control/datapath behavior more substantially instead of making a shallow tweak."
    )


def _unchanged_reviewed_candidate_report(state: AgentState, files: list[dict[str, str]]) -> str:
    previous_files = state.get("candidate_files", [])
    if not previous_files or not state.get("review_feedback_log"):
        return ""
    feedback = _review_feedback_for_coding(state, 4000)
    if feedback == "(none)":
        return ""
    exact_same = generated_files_fingerprint(files) == generated_files_fingerprint(previous_files)
    functional_same = _functional_files_fingerprint(files) == _functional_files_fingerprint(
        previous_files
    )
    if not exact_same and not functional_same:
        return ""
    if exact_same:
        return (
            "Coding team returned RTL identical to the previous candidate after reviewer feedback. "
            "The implementation must revise the existing RTL according to the listed review report; "
            "unchanged files are not accepted as a retry result."
        )
    return (
        "Coding team changed only comments, whitespace, or formatting after reviewer feedback. "
        "The implementation must make a functional RTL change that addresses the listed review report."
    )


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

    previous_hashes = _functional_file_hashes(previous_files)
    current_hashes = _functional_file_hashes(files)
    changed = sorted(
        name
        for name in previous_names
        if previous_hashes.get(name) != current_hashes.get(name)
    )
    if not changed:
        return (
            "Review-driven repair returned all previous files but none changed functionally. "
            "At least one reviewed RTL file must change in behavior to address reviewer findings."
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
    return {
        "generation_ok": False,
        "microarchitecture_passed": False,
        "verification_passed": False,
        "verification_report": report,
        "coding_retry_count": attempt,
        "failed_stage": "coding_repair_contract",
        "blocking_report": report,
        "messages": messages,
        "error_message": report,
        "review_feedback_log": append_review_feedback(
            state, "coding_review_gate", report, task_id
        ),
    }


def _reject_unchanged_candidate(
    state: AgentState,
    task_id: str,
    files: list[dict[str, str]],
    messages: list,
):
    report = _unchanged_reviewed_candidate_report(state, files)
    if not report:
        return None
    attempt = state.get("coding_retry_count", 0) + 1
    print("---VERILOG CODING TEAM: REVIEW FEEDBACK NOT APPLIED; RTL UNCHANGED---")
    write_text_artifact(
        f"failed_attempts/{task_id}_unchanged_after_review_attempt_{attempt}.txt",
        render_files(files) + "\n\n" + report,
    )
    return {
        "generation_ok": False,
        "microarchitecture_passed": False,
        "verification_passed": False,
        "verification_report": report,
        "coding_retry_count": attempt,
        "failed_stage": "coding_unchanged",
        "blocking_report": report,
        "messages": messages,
        "error_message": report,
        "review_feedback_log": append_review_feedback(
            state, "coding_unchanged", report, task_id
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


def _review_gate_report(state: AgentState, files: list[dict[str, str]]) -> str:
    reports = []
    for report in (
        _incomplete_review_repair_report(state, files),
        _unchanged_reviewed_candidate_report(state, files),
        _review_repair_delta_report(state, files),
        _coding_preflight_report(state, files),
    ):
        if report:
            reports.append(report)
    return "\n\n".join(reports)


def _coding_repair_scope_audit(state: AgentState, files: list[dict[str, str]]) -> dict:
    previous_files = state.get("candidate_files", [])
    previous_hashes = _functional_file_hashes(previous_files)
    current_hashes = _functional_file_hashes(files)
    changed_files = sorted(
        name
        for name, previous_hash in previous_hashes.items()
        if current_hashes.get(name) != previous_hash
    )
    new_files = sorted(name for name in current_hashes if name not in previous_hashes)
    removed_files = sorted(name for name in previous_hashes if name not in current_hashes)
    previous_text = _functional_compare_text(previous_files)
    current_text = _functional_compare_text(files)
    if previous_text and current_text:
        ratio = difflib.SequenceMatcher(
            None, previous_text, current_text, autojunk=False
        ).ratio()
        max_len = max(len(previous_text), len(current_text), 1)
        changed_chars = int(max_len * (1.0 - ratio))
    else:
        ratio = 0.0 if current_text else 1.0
        changed_chars = len(current_text)
    return {
        "backlog_count": _coding_backlog_count(state),
        "repair_intensity": _coding_repair_intensity(state),
        "previous_file_count": len(previous_hashes),
        "current_file_count": len(current_hashes),
        "changed_files": changed_files,
        "new_files": new_files,
        "removed_files": removed_files,
        "functional_similarity_ratio": ratio,
        "estimated_changed_functional_chars": changed_chars,
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
    return {
        "generation_ok": False,
        "microarchitecture_passed": False,
        "verification_passed": False,
        "verification_report": report,
        "coding_retry_count": attempt,
        "failed_stage": "coding_review_gate",
        "blocking_report": report,
        "messages": messages,
        "error_message": report,
        "review_feedback_log": append_review_feedback(
            state, "coding_review_gate", report, task_id
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
    print("---VERILOG CODING TEAM: Local review gate failed; invoking focused repair pass---")
    write_text_artifact(
        f"logs/{task_id}_review_gate_failure_attempt_{attempt}.md",
        gate_report,
    )
    repair_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", load_prompt("verilog_review_gate_repair.md")),
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
""",
            ),
        ]
    )
    repair_response = (repair_prompt | llm).invoke(
        {
            "user_request": prompt_payload["user_request"],
            "architecture_contract": prompt_payload["architecture_contract"],
            "task": render_manager_task(task),
            "manager_handoff": prompt_payload["manager_handoff"],
            "supervisor_plan": prompt_payload["supervisor_plan"],
            "control_datapath_plan": prompt_payload["control_datapath_plan"],
            "repair_intensity": prompt_payload["repair_intensity"],
            "coding_action_plan": prompt_payload["coding_action_plan"],
            "previous_candidate_rtl": prompt_payload["previous_candidate_rtl"],
            "current_candidate_rtl": render_files_for_prompt(files, section_limit),
            "coding_repair_backlog": prompt_payload["coding_repair_backlog"],
            "revision_plan": prompt_payload["revision_plan"],
            "repair_brief": prompt_payload["repair_brief"],
            "repair_contract": prompt_payload["repair_contract"],
            "gate_report": gate_report,
            "feedback": prompt_payload["feedback"],
        }
    )
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
        return None, repair_response, str(exc)

    return repaired_files, repair_response, ""


def _repair_candidate_against_review_gate(
    state: AgentState,
    task: dict,
    task_id: str,
    files: list[dict[str, str]],
    prompt_payload: dict,
    section_limit: int,
    messages: list,
):
    gate_report = _review_gate_report(state, files)
    if not gate_report:
        return files, messages, ""

    repaired_files, repair_response, repair_error = _attempt_review_gate_repair(
        state, task, task_id, files, gate_report, prompt_payload, section_limit
    )
    next_messages = messages + ([repair_response] if repair_response else [])
    if repaired_files is None:
        return (
            files,
            next_messages,
            gate_report + f"\n\nAutomated review-gate repair failed: {repair_error}",
        )

    second_report = _review_gate_report(state, repaired_files)
    if second_report:
        return (
            repaired_files,
            next_messages,
            second_report
            + "\n\nAutomated review-gate repair was attempted but did not close all issues.",
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
    return {
        "generation_ok": False,
        "microarchitecture_passed": False,
        "verification_passed": False,
        "verification_report": report,
        "coding_retry_count": attempt,
        "failed_stage": "coding_preflight",
        "blocking_report": report,
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
    prior_backlog = render_coding_repair_backlog(state, state.get("max_context_chars", 120_000))
    lines = [
        "Microarchitecture-to-coding repair packet:",
        f"- task: {task.get('id', task_id)} - {task.get('title', '')}",
        "- verdict: FAIL",
        "- Coding Team must fix both previous unresolved backlog items and this new microarchitecture finding.",
        "- Increase repair scope: update related control logic and datapath logic together, not just one local signal.",
        "",
        "Previously unresolved coding repair backlog:",
        prior_backlog,
        "",
        "Static microarchitecture scan context:",
        str(static_report or "(none)").strip(),
        "",
        "New blocking microarchitecture finding:",
        str(report or "Microarchitecture review failed.").strip(),
        "",
        "Required coding response:",
        "- Revisit every backlog item above and verify whether the current RTL truly fixed it.",
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
        system_prompt = load_prompt("verilog_implementation_repair.md")
        human_prompt = """
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
        system_prompt = load_prompt("verilog_coding.md")
        human_prompt = """
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
        "manager_handoff": clip_text(current_manager_handoff(state), section_limit),
        "supervisor_plan": clip_text(state.get("supervisor_plan") or "(none)", section_limit),
        "control_datapath_plan": clip_text(
            state.get("control_datapath_plan") or "(none)", section_limit
        ),
        "rtl_context": clip_text(state.get("rtl_context") or "(none)", section_limit),
        "revision_mode": _coding_revision_mode(state),
        "repair_intensity": repair_intensity,
        "previous_candidate_manifest": _render_previous_candidate_manifest(
            state.get("candidate_files", [])
        ),
        "previous_candidate_rtl": render_files_for_prompt(
            state.get("candidate_files", []), section_limit
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
        {
            "context_limit": context_limit,
            "section_limit": section_limit,
            "review_driven_repair": review_driven_repair,
            "sections": {
                key: len(str(value))
                for key, value in prompt_payload.items()
            },
        },
    )
    response = (prompt | llm).invoke(prompt_payload)

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
            state, task, task_id, files, prompt_payload, section_limit, [response]
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
            "microarchitecture_passed": False,
            "messages": accepted_messages,
            "failed_stage": "",
            "blocking_report": "",
            "error_message": "",
        }

    exc = initial_error
    print(f"---WARNING: Coding team output failed {initial_error_kind}, attempting repair: {exc}---")
    try:
        repair_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    load_prompt("verilog_coding_repair.md"),
                ),
                (
                    "human",
                    """
Current Manager task:
{task}

Supervisor detailed assignment:
{supervisor_plan}

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
""",
                ),
            ]
        )
        repair_response = (repair_prompt | llm).invoke(
            {
                "task": render_manager_task(task),
                "supervisor_plan": state.get("supervisor_plan") or "(none)",
                "revision_plan": revision_plan,
                "repair_brief": repair_brief,
                "repair_contract": repair_contract,
                "repair_intensity": repair_intensity,
                "coding_action_plan": coding_action_plan,
                "coding_repair_backlog": prompt_payload["coding_repair_backlog"],
                "previous_candidate_rtl": render_files_for_prompt(
                    state.get("candidate_files", []), section_limit
                ),
                "invalid_output": response.content,
                "parser_error": str(exc),
            }
        )
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
                [response, repair_response],
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
        "microarchitecture_passed": False,
        "verification_passed": False,
        "verification_report": report,
        "coding_retry_count": state.get("coding_retry_count", 0) + 1,
        "failed_stage": "coding",
        "blocking_report": report,
        "messages": [response],
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

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                load_prompt("microarchitecture_review.md"),
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

Previous coding repair backlog:
{previous_feedback}

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
            "previous_feedback": previous_feedback,
            "candidate_rtl": render_files_for_prompt(
                merged_files, state.get("max_context_chars", 120_000)
            ),
        }
    )

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
            "microarchitecture_report": report or "PASS",
            "failed_stage": "",
            "blocking_report": "",
            "messages": [response],
        }

    print("---MICROARCH REVIEWER: FAIL---")
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
    return {
        "microarchitecture_passed": False,
        "microarchitecture_report": repair_packet,
        "microarchitecture_retry_count": state.get("microarchitecture_retry_count", 0) + 1,
        "failed_stage": "microarchitecture_review",
        "blocking_report": repair_packet,
        "review_feedback_log": append_review_feedback(
            state,
            "microarchitecture_review",
            repair_packet,
            task_id,
        ),
        "messages": [response],
    }
