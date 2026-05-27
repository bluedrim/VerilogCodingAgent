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
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                load_prompt("supervisor.md"),
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

Previous Supervisor task packet to revise, if any:
{previous_supervisor_plan}

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
            "rtl_context": clip_text(
                state.get("rtl_context") or "(none)",
                state.get("max_context_chars", 120_000),
            ),
            "previous_supervisor_plan": state.get("supervisor_plan") or "(none)",
            "verification_report": state.get("verification_report") or "(none)",
            "review_feedback": review_feedback,
        }
    )
    write_text_artifact(
        f"logs/{task_id}_manager_handoff.md",
        current_manager_handoff(state),
    )
    write_text_artifact(
        f"logs/{task_id}_supervisor_plan.md",
        response.content,
    )
    return {
        "supervisor_plan": response.content,
        "supervisor_review_passed": False,
        "generation_ok": False,
        "verification_passed": False,
        "messages": [response],
    }


@_with_runtime
def supervisor_review_agent(state: AgentState):
    task = current_manager_task(state)
    task_id = sanitize_artifact_name(task.get("id"), "task")
    print(f"---SUPERVISOR REVIEW: Checking task packet for {task['id']}---")
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                load_prompt("supervisor_review.md"),
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

    passed, report = parse_review_result(
        response.content, "Supervisor review output was not valid JSON."
    )

    write_text_artifact(
        f"logs/{task_id}_supervisor_review_raw_attempt_{state.get('supervisor_retry_count', 0) + 1}.txt",
        response.content,
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
            "messages": [response],
        }

    print("---SUPERVISOR REVIEW: FAIL---")
    return {
        "supervisor_review_passed": False,
        "supervisor_review_report": report or "Supervisor task packet is incomplete.",
        "supervisor_retry_count": state.get("supervisor_retry_count", 0) + 1,
        "review_feedback_log": append_review_feedback(
            state,
            "supervisor_review",
            report or "Supervisor task packet is incomplete.",
            task_id,
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
        "review_feedback_log": [],
    }
