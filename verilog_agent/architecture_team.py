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
    system_prompt = load_prompt("architecture.md")
    human_template = """
User requirement:
{user_request}

Manager plan:
{manager_plan}

Architecture revision mode:
{revision_mode}

Previous architecture contract to revise, if any:
{previous_architecture_contract}

Architecture reviewer revision checklist:
{revision_checklist}

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
        "user_request": state["user_request"],
        "manager_plan": render_manager_plan(state["manager_plan"]),
        "previous_architecture_contract": state.get("architecture_contract") or "(none)",
        "revision_mode": revision_mode,
        "revision_checklist": revision_checklist,
        "review_feedback": review_feedback,
    }
    log_agent_prompt(
        "architecture",
        state.get("architecture_retry_count", 0) + 1,
        system_prompt,
        human_template,
        payload,
    )
    response = (prompt | llm).invoke(payload)
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
    system_prompt = load_reviewer_prompt("architecture_review.md")
    human_template = """
Original user requirement:
{user_request}

Manager handoff:
{manager_handoff}

Architecture contract:
{architecture_contract}
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
        "manager_handoff": render_manager_plan(state["manager_plan"]),
        "architecture_contract": state.get("architecture_contract") or "(none)",
    }
    log_agent_prompt(
        "architecture_review",
        state.get("architecture_retry_count", 0) + 1,
        system_prompt,
        human_template,
        payload,
    )
    response = (prompt | llm).invoke(payload)

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
            "architecture_review_forced_forward": False,
            "architecture_review_report": report or "PASS",
            "failed_stage": "",
            "blocking_report": "",
            "messages": [response],
        }

    next_retry_count = state.get("architecture_retry_count", 0) + 1
    force_forward_after = state.get("max_architecture_retries", DEFAULT_MAX_RETRIES)
    force_forward = bool(force_forward_after and next_retry_count >= force_forward_after)
    review_report = report or "Architecture contract is incomplete."
    if force_forward:
        print("---ARCHITECTURE REVIEW: FORCE-FORWARD THRESHOLD REACHED---")
        review_report = (
            "FORCED_FORWARD: Architecture review reached the force-forward threshold. "
            "Proceeding to Supervisor with the best available architecture contract.\n\n"
            + review_report
        )
    else:
        print("---ARCHITECTURE REVIEW: FAIL---")
    return {
        "architecture_review_passed": False,
        "architecture_review_forced_forward": force_forward,
        "architecture_review_report": review_report,
        "architecture_retry_count": next_retry_count,
        "failed_stage": "" if force_forward else "architecture_review",
        "blocking_report": "" if force_forward else review_report,
        "review_feedback_log": append_review_feedback(
            state, "architecture_review", review_report
        ),
        "forced_forward_debt": (
            append_forced_forward_debt(state, "architecture_review", report or review_report, "global")
            if force_forward
            else state.get("forced_forward_debt", [])
        ),
        "messages": [response],
    }
