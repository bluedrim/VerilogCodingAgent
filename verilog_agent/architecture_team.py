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
def architecture_agent(state: AgentState):
    print("---ARCHITECT: Creating RTL Architecture Contract---")
    review_feedback = render_review_feedback(
        state,
        ("architecture_review",),
        state.get("max_context_chars", 120_000),
    )
    if review_feedback != "(none)":
        review_feedback = (
            "Reviewer feedback that must be applied in this revision:\n"
            f"{review_feedback}"
        )
    revision_mode = render_revision_mode(
        state, ("architecture_review",), "architecture contract", "architecture_retry_count"
    )
    revision_checklist = render_revision_checklist(
        state,
        ("architecture_review",),
        "architecture contract",
        state.get("max_context_chars", 120_000),
    )
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                load_prompt("architecture.md"),
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

Architecture revision mode:
{revision_mode}

Previous architecture contract to revise, if any:
{previous_architecture_contract}

Architecture reviewer revision checklist:
{revision_checklist}

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
            "previous_architecture_contract": state.get("architecture_contract") or "(none)",
            "revision_mode": revision_mode,
            "revision_checklist": revision_checklist,
            "review_feedback": review_feedback,
        }
    )
    write_text_artifact("logs/architecture_revision_checklist.md", revision_checklist)
    write_text_artifact("architecture_contract.md", response.content)
    unchanged_report = unchanged_review_revision_report(
        state,
        ("architecture_review",),
        "architecture contract",
        state.get("architecture_contract") or "",
        response.content,
    )
    if unchanged_report:
        write_text_artifact(
            f"failed_attempts/architecture_unchanged_after_review_attempt_{state.get('architecture_retry_count', 0) + 1}.md",
            response.content + "\n\n" + unchanged_report,
        )
        return {
            "architecture_contract": response.content,
            "architecture_review_passed": False,
            "architecture_review_report": unchanged_report,
            "architecture_retry_count": state.get("architecture_retry_count", 0) + 1,
            "failed_stage": "architecture_generation",
            "blocking_report": unchanged_report,
            "review_feedback_log": append_review_feedback(
                state, "architecture_review", unchanged_report
            ),
            "messages": [response],
        }
    return {
        "architecture_contract": response.content,
        "architecture_review_passed": False,
        "failed_stage": "",
        "blocking_report": "",
        "messages": [response],
    }


@_with_runtime
def architecture_review_agent(state: AgentState):
    print("---ARCHITECTURE REVIEW: Checking architecture contract completeness---")
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                load_prompt("architecture_review.md"),
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

    passed, report, decision_details = parse_review_result_with_details(
        response.content, "Architecture review output was not valid JSON."
    )

    write_text_artifact(
        f"logs/architecture_review_raw_attempt_{state.get('architecture_retry_count', 0) + 1}.txt",
        response.content,
    )
    write_json_artifact(
        f"logs/architecture_review_decision_attempt_{state.get('architecture_retry_count', 0) + 1}.json",
        decision_details,
    )
    write_text_artifact(
        f"logs/architecture_review_attempt_{state.get('architecture_retry_count', 0) + 1}.md",
        report or ("PASS" if passed else "FAIL"),
    )

    if passed:
        print("---ARCHITECTURE REVIEW: PASS---")
        return {
            "architecture_review_passed": True,
            "architecture_review_report": report or "PASS",
            "failed_stage": "",
            "blocking_report": "",
            "messages": [response],
        }

    print("---ARCHITECTURE REVIEW: FAIL---")
    return {
        "architecture_review_passed": False,
        "architecture_review_report": report or "Architecture contract is incomplete.",
        "architecture_retry_count": state.get("architecture_retry_count", 0) + 1,
        "failed_stage": "architecture_review",
        "blocking_report": report or "Architecture contract is incomplete.",
        "review_feedback_log": append_review_feedback(
            state, "architecture_review", report or "Architecture contract is incomplete."
        ),
        "messages": [response],
    }
