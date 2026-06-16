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
def control_datapath_planner_agent(state: AgentState):
    task = current_manager_task(state)
    task_id = sanitize_artifact_name(task.get("id"), "task")
    print(f"---CONTROL/DATAPATH PLANNER: Structuring {task['id']}---")
    review_feedback = render_review_feedback(
        state,
        ("supervisor_review", "control_datapath_review", "microarchitecture_review", "verification"),
        state.get("max_context_chars", 120_000),
    )
    if review_feedback != "(none)":
        review_feedback = (
            "Reviewer feedback that must be reflected in this Control/Data Path plan:\n"
            f"{review_feedback}"
        )
    revision_mode = render_revision_mode(
        state,
        ("supervisor_review", "control_datapath_review", "microarchitecture_review", "verification"),
        "Control/Data Path plan",
        "control_datapath_retry_count",
    )
    revision_checklist = render_revision_checklist(
        state,
        ("supervisor_review", "control_datapath_review", "microarchitecture_review", "verification"),
        "Control/Data Path plan",
        state.get("max_context_chars", 120_000),
    )
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                load_prompt("control_datapath_planner.md"),
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

Control/Data Path revision mode:
{revision_mode}

Existing RTL context:
{rtl_context}

Previous Control/Data Path plan to revise, if any:
{previous_control_datapath_plan}

Previous verification report, if any:
{verification_report}

Control/Data Path reviewer revision checklist:
{revision_checklist}

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
            "rtl_context": clip_text(
                state.get("rtl_context") or "(none)",
                state.get("max_context_chars", 120_000),
            ),
            "previous_control_datapath_plan": state.get("control_datapath_plan") or "(none)",
            "verification_report": state.get("verification_report") or "(none)",
            "revision_mode": revision_mode,
            "revision_checklist": revision_checklist,
            "review_feedback": review_feedback,
        }
    )
    write_text_artifact(
        f"logs/{task_id}_control_datapath_plan.md",
        response.content,
    )
    write_text_artifact(
        f"logs/{task_id}_control_datapath_revision_checklist.md",
        revision_checklist,
    )
    unchanged_report = unchanged_review_revision_report(
        state,
        ("supervisor_review", "control_datapath_review", "microarchitecture_review", "verification"),
        "Control/Data Path plan",
        state.get("control_datapath_plan") or "",
        response.content,
    )
    if unchanged_report:
        write_text_artifact(
            f"failed_attempts/{task_id}_control_datapath_unchanged_after_review_attempt_{state.get('control_datapath_retry_count', 0) + 1}.md",
            response.content + "\n\n" + unchanged_report,
        )
        return {
            "control_datapath_plan": response.content,
            "control_datapath_review_passed": False,
            "control_datapath_review_report": unchanged_report,
            "control_datapath_retry_count": state.get("control_datapath_retry_count", 0) + 1,
            "failed_stage": "control_datapath_generation",
            "blocking_report": unchanged_report,
            "review_feedback_log": append_review_feedback(
                state, "control_datapath_review", unchanged_report, task_id
            ),
            "messages": [response],
        }
    return {
        "control_datapath_plan": response.content,
        "control_datapath_review_passed": False,
        "failed_stage": "",
        "blocking_report": "",
        "messages": [response],
    }


@_with_runtime
def control_datapath_review_agent(state: AgentState):
    task = current_manager_task(state)
    task_id = sanitize_artifact_name(task.get("id"), "task")
    print(f"---CONTROL/DATAPATH REVIEW: Checking micro-architecture plan for {task['id']}---")
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                load_prompt("control_datapath_review.md"),
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

    passed, report, decision_details = parse_review_result_with_details(
        response.content, "Control/Data Path review output was not valid JSON."
    )

    write_text_artifact(
        f"logs/{task_id}_control_datapath_review_raw_attempt_{state.get('control_datapath_retry_count', 0) + 1}.txt",
        response.content,
    )
    write_json_artifact(
        f"logs/{task_id}_control_datapath_review_decision_attempt_{state.get('control_datapath_retry_count', 0) + 1}.json",
        decision_details,
    )
    write_text_artifact(
        f"logs/{task_id}_control_datapath_review_attempt_{state.get('control_datapath_retry_count', 0) + 1}.md",
        report or ("PASS" if passed else "FAIL"),
    )

    if passed:
        print("---CONTROL/DATAPATH REVIEW: PASS---")
        return {
            "control_datapath_review_passed": True,
            "control_datapath_review_forced_forward": False,
            "control_datapath_review_report": report or "PASS",
            "failed_stage": "",
            "blocking_report": "",
            "messages": [response],
        }

    next_retry_count = state.get("control_datapath_retry_count", 0) + 1
    force_forward_after = state.get("max_control_datapath_retries", 10)
    force_forward = bool(force_forward_after and next_retry_count >= force_forward_after)
    review_report = report or "Control/Data Path plan is incomplete."
    if force_forward:
        print("---CONTROL/DATAPATH REVIEW: FORCE-FORWARD THRESHOLD REACHED---")
        review_report = (
            "FORCED_FORWARD: Control/Data Path review reached the force-forward threshold. "
            "Proceeding to Coding with the best available control/datapath plan.\n\n"
            + review_report
        )
    else:
        print("---CONTROL/DATAPATH REVIEW: FAIL---")
    return {
        "control_datapath_review_passed": False,
        "control_datapath_review_forced_forward": force_forward,
        "control_datapath_review_report": review_report,
        "control_datapath_retry_count": next_retry_count,
        "failed_stage": "" if force_forward else "control_datapath_review",
        "blocking_report": "" if force_forward else review_report,
        "review_feedback_log": append_review_feedback(
            state,
            "control_datapath_review",
            review_report,
            task_id,
        ),
        "messages": [response],
    }
