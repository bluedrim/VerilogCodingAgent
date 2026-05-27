from __future__ import annotations

from .runtime import refresh_globals


def _with_runtime(fn):
    def wrapped(*args, **kwargs):
        refresh_globals(globals())
        return fn(*args, **kwargs)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


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
        write_text_artifact(
            f"failed_attempts/{task_id}_sanity_failed_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
            render_files(state.get("candidate_files", [])) + "\n\n" + report,
        )
        print("---VERIFICATION TEAM: BASIC SANITY FAIL---")
        return {
            "verification_passed": False,
            "verification_report": report,
            "verification_retry_count": state.get("verification_retry_count", 0) + 1,
            "failed_stage": "verification",
            "blocking_report": report,
            "review_feedback_log": append_review_feedback(state, "verification", report, task_id),
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
        write_text_artifact(
            f"failed_attempts/{task_id}_lint_failed_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
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
            "review_feedback_log": append_review_feedback(
                state, "verification_lint", report, task_id
            ),
        }

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                load_prompt("verification.md"),
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
            "candidate_rtl": render_files_for_prompt(
                merged_files, state.get("max_context_chars", 120_000)
            ),
        }
    )

    passed, report = parse_review_result(
        response.content,
        "Verification output was not valid JSON. Re-run coding with clearer, self-checkable RTL.",
    )

    write_text_artifact(
        f"logs/{task_id}_verification_raw_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
        response.content,
    )
    if passed:
        print("---VERIFICATION TEAM: PASS---")
        write_text_artifact(
            f"logs/{task_id}_verification_report.md",
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
        f"failed_attempts/{task_id}_functional_failed_attempt_{state.get('verification_retry_count', 0) + 1}.txt",
        render_files(state.get("candidate_files", [])) + "\n\n" + report,
    )
    return {
        "verification_passed": False,
        "verification_report": report or "Verification failed without a detailed report.",
        "lint_report": lint_result["report"],
        "verification_retry_count": state.get("verification_retry_count", 0) + 1,
        "failed_stage": "verification",
        "blocking_report": report or "Verification failed without a detailed report.",
        "review_feedback_log": append_review_feedback(
            state,
            "verification",
            report or "Verification failed without a detailed report.",
            task_id,
        ),
        "messages": [response],
    }
