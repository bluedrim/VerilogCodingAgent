from __future__ import annotations

from .runtime import refresh_globals


def _with_runtime(fn):
    def wrapped(*args, **kwargs):
        refresh_globals(globals())
        return fn(*args, **kwargs)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


def _render_candidate_summary(files: list[dict[str, str]]) -> str:
    if not files:
        return "(no candidate files)"
    lines = ["Candidate RTL structural summary:"]
    lines.append("Files:")
    for item in build_file_manifest(files):
        sha = str(item.get("sha256", ""))
        lines.append(f"- {item.get('filename')}: {item.get('bytes')} bytes, sha256={sha[:12]}")
    modules = extract_module_names(files)
    top_candidates = infer_top_module_candidates(files)
    instances = extract_instantiated_modules(files)
    lines.append(f"Defined modules: {', '.join(modules) or '(none)'}")
    lines.append(f"Likely top modules: {', '.join(top_candidates) or '(unknown)'}")
    lines.append(f"Instantiated modules: {', '.join(instances) or '(none)'}")
    return "\n".join(lines)


def _render_verification_repair_packet(
    state: AgentState,
    task_id: str,
    stage: str,
    report: str,
    files: list[dict[str, str]],
    lint_report: str = "",
) -> str:
    task = current_manager_task(state)
    prior_backlog = render_coding_repair_backlog(state, state.get("max_context_chars", 120_000))
    lines = [
        "Verification-to-coding repair packet:",
        f"- stage: {stage}",
        f"- task: {task.get('id', task_id)} - {task.get('title', '')}",
        "- verdict: FAIL",
        "- Coding Team must change RTL behavior or structure; comments/formatting alone are not a fix.",
        "- Coding Team must fix both previous unresolved backlog items and the new finding in one coordinated RTL update.",
        "- Preserve module interfaces unless this packet explicitly identifies an interface mismatch.",
        "",
        _render_candidate_summary(files),
        "",
        "Previously unresolved coding repair backlog:",
        prior_backlog,
        "",
        "New blocking verification finding:",
        str(report or "Verification failed without a detailed report.").strip(),
    ]
    if lint_report:
        lines.extend(["", "Syntax/lint context:", str(lint_report).strip()])
    lines.extend(
        [
            "",
            "Required coding response:",
            "- Locate the affected file/module/signal named above, or infer it from the candidate summary.",
            "- Revisit every backlog item above and modify the RTL so the next reviewer cannot repeat it.",
            "- Modify the relevant reset, FSM, control output, handshake, datapath register, width handling, or interface code.",
            "- Return complete Verilog-2001 files for every reviewed candidate file.",
            "- Ensure the same verification finding would not be repeated on the next attempt.",
        ]
    )
    return "\n".join(lines)


@_with_runtime
def verification_team_agent(state: AgentState):
    task = current_manager_task(state)
    task_id = sanitize_artifact_name(task.get("id"), "task")
    print(f"---VERIFICATION TEAM: Checking {task['id']}---")
    merged_files = merge_files(state.get("final_files", []), state.get("candidate_files", []))
    sanity_result = basic_rtl_sanity(merged_files, state.get("allow_blackboxes", False))
    write_text_artifact(
        f"logs/{task_id}_basic_sanity_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
        sanity_result["report"],
    )
    if not sanity_result["passed"]:
        report = f"Basic RTL sanity failed before lint:\n{sanity_result['report']}"
        repair_packet = _render_verification_repair_packet(
            state, task_id, "verification_sanity", report, state.get("candidate_files", [])
        )
        write_text_artifact(
            f"failed_attempts/{task_id}_sanity_failed_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
            render_files(state.get("candidate_files", [])) + "\n\n" + repair_packet,
        )
        write_text_artifact(
            f"logs/{task_id}_verification_repair_packet_attempt_{state.get('verification_retry_count', 0) + 1}.md",
            repair_packet,
        )
        next_retry_count = state.get("verification_retry_count", 0) + 1
        force_forward_after = state.get("max_retries", DEFAULT_MAX_RETRIES)
        force_forward = bool(force_forward_after and next_retry_count >= force_forward_after)
        if force_forward:
            print("---VERIFICATION TEAM: FORCE-FORWARD THRESHOLD REACHED AFTER BASIC SANITY FAIL---")
            repair_packet = (
                "FORCED_FORWARD: Verification sanity review reached the force-forward threshold. "
                "Accepting the best available RTL candidate for this task.\n\n"
                + repair_packet
            )
        else:
            print("---VERIFICATION TEAM: BASIC SANITY FAIL---")
        return {
            "verification_passed": False,
            "verification_review_forced_forward": force_forward,
            "verification_report": repair_packet,
            "verification_retry_count": next_retry_count,
            "failed_stage": "" if force_forward else "verification",
            "blocking_report": "" if force_forward else repair_packet,
            "review_feedback_log": append_review_feedback(state, "verification", repair_packet, task_id),
        }

    lint_result = run_syntax_lint(
        merged_files,
        state.get("require_lint", False),
        state.get("lint_timeout_seconds", 30),
    )
    write_text_artifact(
        f"logs/{task_id}_lint_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
        lint_result["report"],
    )
    if not lint_result["passed"]:
        report = f"Syntax lint failed before functional review:\n{lint_result['report']}"
        repair_packet = _render_verification_repair_packet(
            state,
            task_id,
            "verification_lint",
            report,
            state.get("candidate_files", []),
            lint_result["report"],
        )
        write_text_artifact(
            f"failed_attempts/{task_id}_lint_failed_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
            render_files(state.get("candidate_files", [])) + "\n\n" + repair_packet,
        )
        write_text_artifact(
            f"logs/{task_id}_verification_repair_packet_attempt_{state.get('verification_retry_count', 0) + 1}.md",
            repair_packet,
        )
        next_retry_count = state.get("verification_retry_count", 0) + 1
        force_forward_after = state.get("max_retries", DEFAULT_MAX_RETRIES)
        force_forward = bool(force_forward_after and next_retry_count >= force_forward_after)
        if force_forward:
            print("---VERIFICATION TEAM: FORCE-FORWARD THRESHOLD REACHED AFTER LINT FAIL---")
            repair_packet = (
                "FORCED_FORWARD: Verification lint review reached the force-forward threshold. "
                "Accepting the best available RTL candidate for this task.\n\n"
                + repair_packet
            )
        else:
            print("---VERIFICATION TEAM: LINT FAIL---")
        return {
            "verification_passed": False,
            "verification_review_forced_forward": force_forward,
            "verification_report": repair_packet,
            "lint_report": lint_result["report"],
            "verification_retry_count": next_retry_count,
            "failed_stage": "" if force_forward else "verification_lint",
            "blocking_report": "" if force_forward else repair_packet,
            "review_feedback_log": append_review_feedback(
                state, "verification_lint", repair_packet, task_id
            ),
        }

    candidate_summary = _render_candidate_summary(merged_files)
    previous_feedback = render_coding_repair_backlog(
        state, state.get("max_context_chars", 120_000)
    )
    write_text_artifact(
        f"logs/{task_id}_verification_candidate_summary_attempt_{state.get('verification_retry_count', 0) + 1}.md",
        candidate_summary,
    )

    system_prompt = load_prompt("verification.md")
    human_template = """
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

Candidate structural summary:
{candidate_summary}

Previous verification/coding feedback, if any:
{previous_feedback}

RTL candidate to verify:
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
        "user_request": state["user_request"],
        "architecture_contract": state.get("architecture_contract") or "(none)",
        "task": render_manager_task(task),
        "manager_handoff": current_manager_handoff(state),
        "supervisor_plan": state["supervisor_plan"],
        "control_datapath_plan": state.get("control_datapath_plan") or "(none)",
        "candidate_summary": candidate_summary,
        "previous_feedback": previous_feedback,
        "candidate_rtl": render_files_for_prompt(
            merged_files, state.get("max_context_chars", 120_000)
        ),
    }
    log_agent_prompt(
        "verification",
        f"{task_id}_{state.get('verification_retry_count', 0) + 1}",
        system_prompt,
        human_template,
        payload,
    )
    response = (prompt | llm).invoke(payload)

    passed, report, decision_details = parse_review_result_with_details(
        response.content,
        "Verification output was not valid JSON. Re-run coding with clearer, self-checkable RTL.",
    )

    write_text_artifact(
        f"logs/{task_id}_verification_raw_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
        response.content,
    )
    write_json_artifact(
        f"logs/{task_id}_verification_decision_attempt_{state.get('verification_retry_count', 0) + 1}.json",
        decision_details,
    )
    if passed:
        print("---VERIFICATION TEAM: PASS---")
        write_text_artifact(
            f"logs/{task_id}_verification_report.md",
            report or "PASS",
        )
        return {
            "verification_passed": True,
            "verification_review_forced_forward": False,
            "verification_report": report or "PASS",
            "lint_report": lint_result["report"],
            "failed_stage": "",
            "blocking_report": "",
            "messages": [response],
        }

    print("---VERIFICATION TEAM: FAIL---")
    repair_packet = _render_verification_repair_packet(
        state,
        task_id,
        "verification",
        report or "Verification failed without a detailed report.",
        state.get("candidate_files", []),
        lint_result["report"],
    )
    write_text_artifact(
        f"failed_attempts/{task_id}_functional_failed_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
        render_files(state.get("candidate_files", [])) + "\n\n" + repair_packet,
    )
    write_text_artifact(
        f"logs/{task_id}_verification_repair_packet_attempt_{state.get('verification_retry_count', 0) + 1}.md",
        repair_packet,
    )
    next_retry_count = state.get("verification_retry_count", 0) + 1
    force_forward_after = state.get("max_retries", DEFAULT_MAX_RETRIES)
    force_forward = bool(force_forward_after and next_retry_count >= force_forward_after)
    if force_forward:
        print("---VERIFICATION TEAM: FORCE-FORWARD THRESHOLD REACHED---")
        repair_packet = (
            "FORCED_FORWARD: Verification review reached the force-forward threshold. "
            "Accepting the best available RTL candidate for this task.\n\n"
            + repair_packet
        )
    return {
        "verification_passed": False,
        "verification_review_forced_forward": force_forward,
        "verification_report": repair_packet,
        "lint_report": lint_result["report"],
        "verification_retry_count": next_retry_count,
        "failed_stage": "" if force_forward else "verification",
        "blocking_report": "" if force_forward else repair_packet,
        "review_feedback_log": append_review_feedback(
            state,
            "verification",
            repair_packet,
            task_id,
        ),
        "messages": [response],
    }
