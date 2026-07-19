from __future__ import annotations

from .runtime import refresh_globals


def _with_runtime(fn):
    def wrapped(*args, **kwargs):
        refresh_globals(globals())
        return fn(*args, **kwargs)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


SUPERVISOR_REQUIRED_SECTIONS = (
    "Task Objective",
    "Source Trace",
    "File and Module Impact",
    "Interface and Parameter Contract",
    "Control/Data Path Assignment",
    "Sequencing and Timing",
    "Edge Cases and Error Handling",
    "Implementation Checklist",
    "Verification Checklist",
    "Handoff Notes",
)


def _section_present(packet: str, section: str) -> bool:
    pattern = rf"(?im)^\s*(?:#+\s*)?(?:\d+\.\s*)?{re.escape(section)}\b"
    return bool(re.search(pattern, packet or ""))


def _supervisor_packet_quality_report(packet: str) -> str:
    text = packet or ""
    stripped = text.strip()
    blockers = []
    if len(stripped) < 1200:
        blockers.append("Supervisor packet is too brief to be implementation-ready.")

    missing_sections = [
        section for section in SUPERVISOR_REQUIRED_SECTIONS if not _section_present(text, section)
    ]
    if missing_sections:
        blockers.append("Missing required sections: " + ", ".join(missing_sections))

    quality_checks = [
        ("interface contract", ("input", "output", "width", "reset", "clock", "clk", "rst")),
        ("control/datapath assignment", ("control", "datapath", "enable", "state", "done", "valid", "ready")),
        ("timing behavior", ("cycle", "latency", "throughput", "reset release", "backpressure")),
        ("edge/error behavior", ("overflow", "underflow", "invalid", "boundary", "error")),
        ("Verilog-2001 coding constraint", ("verilog-2001", ".v", "reg/wire", "always @(*)")),
        ("verification checklist", ("check", "verify", "corner", "reset", "expected")),
    ]
    lowered = stripped.lower()
    for label, terms in quality_checks:
        if not any(term in lowered for term in terms):
            blockers.append(f"Missing concrete {label} details.")

    if not blockers:
        return ""
    return "Supervisor packet preflight failed:\n" + "\n".join(f"- {item}" for item in blockers)


def _render_supervisor_repair_contract(
    state: AgentState,
    task: dict,
    revision_checklist: str,
    preflight_report: str = "",
) -> str:
    lines = [
        "Supervisor repair contract:",
        "- The Supervisor packet is the binding input for Control/Data Path planning and RTL coding.",
        "- Convert every review/preflight issue into explicit downstream implementation instructions.",
        "- Do not answer with general design advice; fill concrete signal, timing, reset, control, datapath, edge-case, and verification details.",
        "- Preserve correct prior content, but revise weak sections directly.",
        "",
        "Current Manager task:",
        render_manager_task(task),
        "",
        "Required Supervisor sections:",
    ]
    lines.extend(f"- {section}" for section in SUPERVISOR_REQUIRED_SECTIONS)
    if revision_checklist != "(none)":
        lines.extend(["", "Review checklist that must be closed:", revision_checklist])
    if preflight_report:
        lines.extend(["", "Local Supervisor preflight findings to close:", preflight_report])
    lines.extend(
        [
            "",
            "Minimum concreteness requirements:",
            "- Name files/modules to create or modify.",
            "- Name ports/signals with direction, width, reset value, and clock domain when applicable.",
            "- Specify control logic responsibilities and datapath responsibilities separately.",
            "- Specify cycle-level behavior, latency/throughput, reset release, and backpressure or mark N/A with reason.",
            "- Specify edge/error behavior and boundary cases.",
            "- Provide coding checklist items that can be implemented in synthesizable Verilog-2001.",
            "- Provide verification checklist items that can be checked by the Verification Team.",
        ]
    )
    return "\n".join(lines)


@_with_runtime
def supervisor_agent(state: AgentState):
    task = current_manager_task(state)
    task_id = sanitize_artifact_name(task.get("id"), "task")
    print(f"---SUPERVISOR: Detailing Task {task['id']} - {task['title']}---")
    review_feedback = render_review_feedback(
        state,
        ("architecture_review", "supervisor_review", "verification"),
        state.get("max_context_chars", 120_000),
    )
    if review_feedback != "(none)":
        review_feedback = (
            "Reviewer feedback that must be applied in this Supervisor task packet:\n"
            f"{review_feedback}"
        )
    revision_mode = render_revision_mode(
        state,
        ("architecture_review", "supervisor_review", "verification"),
        "Supervisor task packet",
        "supervisor_retry_count",
    )
    revision_checklist = render_revision_checklist(
        state,
        ("architecture_review", "supervisor_review", "verification"),
        "Supervisor task packet",
        state.get("max_context_chars", 120_000),
    )
    repair_contract = _render_supervisor_repair_contract(state, task, revision_checklist)
    system_prompt = load_prompt("supervisor.md")
    human_template = """
Current Manager task packet:
{manager_handoff}

Architecture contract:
{architecture_contract}

Supervisor revision mode:
{revision_mode}

Existing RTL context:
{rtl_context}

Previous Supervisor task packet to revise, if any:
{previous_supervisor_plan}

Previous verification report, if any:
{verification_report}

Supervisor reviewer revision checklist:
{revision_checklist}

Supervisor repair contract:
{repair_contract}

{review_feedback}
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
        "manager_handoff": current_manager_handoff(state),
        "architecture_contract": state.get("architecture_contract") or "(none)",
        "rtl_context": clip_text(
            state.get("rtl_context") or "(none)",
            state.get("max_context_chars", 120_000),
        ),
        "previous_supervisor_plan": state.get("supervisor_plan") or "(none)",
        "verification_report": state.get("verification_report") or "(none)",
        "revision_mode": revision_mode,
        "revision_checklist": revision_checklist,
        "repair_contract": repair_contract,
        "review_feedback": review_feedback,
    }
    log_agent_prompt(
        "supervisor",
        f"{task_id}_{state.get('supervisor_retry_count', 0) + 1}",
        system_prompt,
        human_template,
        payload,
    )
    response = (prompt | llm).invoke(payload)
    write_text_artifact(
        f"logs/{task_id}_manager_handoff.md",
        current_manager_handoff(state),
    )
    write_text_artifact(
        f"logs/{task_id}_supervisor_plan.md",
        response.content,
    )
    write_text_artifact(
        f"logs/{task_id}_supervisor_revision_checklist.md",
        revision_checklist,
    )
    write_text_artifact(
        f"logs/{task_id}_supervisor_repair_contract.md",
        repair_contract,
    )
    unchanged_report = unchanged_review_revision_report(
        state,
        ("architecture_review", "supervisor_review", "verification"),
        "Supervisor task packet",
        state.get("supervisor_plan") or "",
        response.content,
    )
    if unchanged_report:
        write_text_artifact(
            f"failed_attempts/{task_id}_supervisor_unchanged_after_review_attempt_{state.get('supervisor_retry_count', 0) + 1}.md",
            response.content + "\n\n" + unchanged_report,
        )
        return {
            "supervisor_plan": response.content,
            "supervisor_review_passed": False,
            "supervisor_review_report": unchanged_report,
            "supervisor_retry_count": state.get("supervisor_retry_count", 0) + 1,
            "generation_ok": False,
            "verification_passed": False,
            "failed_stage": "supervisor_generation",
            "blocking_report": unchanged_report,
            "review_feedback_log": append_review_feedback(
                state, "supervisor_review", unchanged_report, task_id
            ),
            "messages": [response],
        }
    preflight_report = _supervisor_packet_quality_report(response.content)
    if preflight_report:
        repair_contract = _render_supervisor_repair_contract(
            state, task, revision_checklist, preflight_report
        )
        write_text_artifact(
            f"failed_attempts/{task_id}_supervisor_preflight_failed_attempt_{state.get('supervisor_retry_count', 0) + 1}.md",
            response.content + "\n\n" + repair_contract,
        )
        write_text_artifact(
            f"logs/{task_id}_supervisor_repair_contract.md",
            repair_contract,
        )
        return {
            "supervisor_plan": response.content,
            "supervisor_review_passed": False,
            "supervisor_review_report": preflight_report,
            "supervisor_retry_count": state.get("supervisor_retry_count", 0) + 1,
            "generation_ok": False,
            "verification_passed": False,
            "failed_stage": "supervisor_generation",
            "blocking_report": preflight_report,
            "review_feedback_log": append_review_feedback(
                state, "supervisor_review", preflight_report, task_id
            ),
            "messages": [response],
        }
    return {
        "supervisor_plan": response.content,
        "supervisor_review_passed": False,
        "generation_ok": False,
        "verification_passed": False,
        "failed_stage": "",
        "blocking_report": "",
        "messages": [response],
    }


@_with_runtime
def supervisor_review_agent(state: AgentState):
    task = current_manager_task(state)
    task_id = sanitize_artifact_name(task.get("id"), "task")
    print(f"---SUPERVISOR REVIEW: Checking task packet for {task['id']}---")
    system_prompt = load_reviewer_prompt("supervisor_review.md")
    human_template = """
Original user requirement:
{user_request}

Manager handoff:
{manager_handoff}

Architecture contract:
{architecture_contract}

Supervisor task packet:
{supervisor_plan}
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
        "user_request": state["user_request"],
        "manager_handoff": current_manager_handoff(state),
        "architecture_contract": state.get("architecture_contract") or "(none)",
        "supervisor_plan": state.get("supervisor_plan") or "(none)",
    }
    log_agent_prompt(
        "supervisor_review",
        f"{task_id}_{state.get('supervisor_retry_count', 0) + 1}",
        system_prompt,
        human_template,
        payload,
    )
    response = (prompt | llm).invoke(payload)

    passed, report, decision_details = parse_review_result_with_details(
        response.content, "Supervisor review output was not valid JSON."
    )

    write_text_artifact(
        f"logs/{task_id}_supervisor_review_raw_attempt_{state.get('supervisor_retry_count', 0) + 1}.txt",
        response.content,
    )
    write_json_artifact(
        f"logs/{task_id}_supervisor_review_decision_attempt_{state.get('supervisor_retry_count', 0) + 1}.json",
        decision_details,
    )
    write_text_artifact(
        f"logs/{task_id}_supervisor_review_attempt_{state.get('supervisor_retry_count', 0) + 1}.md",
        report or ("PASS" if passed else "FAIL"),
    )

    if passed:
        print("---SUPERVISOR REVIEW: PASS---")
        return {
            "supervisor_review_passed": True,
            "supervisor_review_report": report or "PASS",
            "supervisor_review_forced_forward": False,
            "failed_stage": "",
            "blocking_report": "",
            "messages": [response],
        }

    print("---SUPERVISOR REVIEW: FAIL---")
    review_report = report or "Supervisor task packet is incomplete."
    next_retry_count = state.get("supervisor_retry_count", 0) + 1
    force_forward_after = state.get("max_supervisor_retries", DEFAULT_MAX_RETRIES)
    force_forward = bool(force_forward_after and next_retry_count >= force_forward_after)
    if force_forward:
        review_report = (
            f"{review_report}\n\n"
            "FORCED_FORWARD: Supervisor review reached the force-forward threshold. "
            "Proceeding to Control/Data Path planning with the best available Supervisor task packet; "
            "downstream teams must treat the remaining review issues as advisory constraints to close."
        )
    return {
        "supervisor_review_passed": False,
        "supervisor_review_report": review_report,
        "supervisor_review_forced_forward": force_forward,
        "supervisor_retry_count": next_retry_count,
        "failed_stage": "" if force_forward else "supervisor_review",
        "blocking_report": "" if force_forward else review_report,
        "review_feedback_log": append_review_feedback(
            state,
            "supervisor_review",
            review_report,
            task_id,
        ),
        "forced_forward_debt": (
            append_forced_forward_debt(state, "supervisor_review", report or review_report, task_id)
            if force_forward
            else state.get("forced_forward_debt", [])
        ),
        "messages": [response],
    }


@_with_runtime
def supervisor_accept_agent(state: AgentState):
    task = current_manager_task(state)
    task_id = sanitize_artifact_name(task.get("id"), "task")
    print(f"---SUPERVISOR: Accepting {task['id']} and Preparing Next Task---")
    final_files = merge_files(state.get("final_files", []), state.get("candidate_files", []))
    rtl_context = render_files(final_files)
    top_module_candidates = infer_top_module_candidates(final_files)
    write_json_artifact(f"logs/{task_id}_accepted_files.json", final_files)
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
        "coding_review_forced_forward": False,
        "verification_passed": False,
        "verification_review_forced_forward": False,
        "verification_report": "",
        "lint_report": "",
        "supervisor_plan": "",
        "supervisor_review_passed": False,
        "supervisor_review_report": "",
        "supervisor_review_forced_forward": False,
        "supervisor_retry_count": 0,
        "control_datapath_plan": "",
        "control_datapath_review_passed": False,
        "control_datapath_review_report": "",
        "control_datapath_review_forced_forward": False,
        "control_datapath_retry_count": 0,
        "microarchitecture_passed": False,
        "microarchitecture_review_forced_forward": False,
        "microarchitecture_report": "",
        "failed_stage": "",
        "blocking_report": "",
        "review_feedback_log": [],
    }
