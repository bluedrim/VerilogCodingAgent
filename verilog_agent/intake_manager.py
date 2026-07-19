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
    max_chars = state.get("max_user_request_chars", 200_000)
    if max_chars and len(user_request) > max_chars:
        message = (
            f"User requirement has {len(user_request)} characters, above limit {max_chars}. "
            "Use --max-user-request-chars to raise the limit."
        )
        write_text_artifact("failed_attempts/user_requirement_too_large.txt", message)
        sys.exit(message)
    write_text_artifact("user_requirement.txt", user_request)
    write_json_artifact("llm_config.json", active_llm_config)
    return {
        "user_request": user_request,
        "messages": [HumanMessage(content=f"User RTL requirement: {user_request}")],
    }


def _review_and_repair_manager_plan(state: AgentState, plan: list[dict]):
    messages = []
    last_report = ""
    configured_limit = int(state.get("max_manager_retries", DEFAULT_MAX_RETRIES) or 0)
    attempt_limit = configured_limit if configured_limit > 0 else DEFAULT_MAX_RETRIES

    for attempt in range(1, attempt_limit + 1):
        review_system_prompt = load_reviewer_prompt("manager_review.md")
        review_human_template = """
Original user requirement:
{user_request}

Ordered Manager plan:
{manager_plan}
"""
        review_payload = {
            "user_request": state["user_request"],
            "manager_plan": json.dumps(plan, ensure_ascii=False, indent=2),
        }
        review_prompt = ChatPromptTemplate.from_messages(
            [("system", review_system_prompt), ("human", review_human_template)]
        )
        log_agent_prompt(
            "manager_review",
            attempt,
            review_system_prompt,
            review_human_template,
            review_payload,
        )
        review_response = (review_prompt | llm).invoke(review_payload)
        messages.append(review_response)
        passed, report, decision_details = parse_review_result_with_details(
            review_response.content,
            "Manager semantic review output was not valid JSON.",
        )
        write_text_artifact(
            f"logs/manager_review_raw_attempt_{attempt}.txt", review_response.content
        )
        write_json_artifact(
            f"logs/manager_review_decision_attempt_{attempt}.json", decision_details
        )
        write_text_artifact(
            f"logs/manager_review_attempt_{attempt}.md", report or ("PASS" if passed else "FAIL")
        )
        if passed:
            return plan, messages, "", attempt

        last_report = report or "Manager plan has a blocking semantic defect."
        if attempt >= attempt_limit:
            break

        repair_system_prompt = load_prompt("manager_plan_repair.md")
        repair_human_template = """
Original user requirement:
{user_request}

Previous Manager plan JSON:
{manager_plan}

Manager review blocking findings:
{review_report}
"""
        repair_payload = {
            "user_request": state["user_request"],
            "manager_plan": json.dumps(plan, ensure_ascii=False, indent=2),
            "review_report": last_report,
        }
        repair_prompt = ChatPromptTemplate.from_messages(
            [("system", repair_system_prompt), ("human", repair_human_template)]
        )
        log_agent_prompt(
            "manager_plan_repair",
            attempt,
            repair_system_prompt,
            repair_human_template,
            repair_payload,
        )
        repair_response = (repair_prompt | llm).invoke(repair_payload)
        messages.append(repair_response)
        write_text_artifact(
            f"logs/manager_semantic_repair_raw_attempt_{attempt}.txt",
            repair_response.content,
        )
        try:
            plan = parse_manager_plan_response(
                repair_response.content, state.get("max_manager_tasks", 32)
            )
        except (json.JSONDecodeError, ValueError) as exc:
            last_report = f"Manager semantic repair returned an invalid plan: {exc}"

    return plan, messages, last_report, attempt_limit


def _manager_success_update(
    state: AgentState,
    plan: list[dict],
    messages: list,
    preceding_error: str = "",
):
    reviewed_plan, review_messages, semantic_debt, review_attempts = (
        _review_and_repair_manager_plan(state, plan)
    )
    write_json_artifact("manager_plan.json", reviewed_plan)
    if semantic_debt:
        write_text_artifact("failed_attempts/manager_semantic_review_forced_forward.md", semantic_debt)
    error_parts = [part for part in (preceding_error, semantic_debt) if part]
    return {
        "manager_plan": reviewed_plan,
        "current_task_index": 0,
        "manager_review_passed": not bool(semantic_debt),
        "manager_review_report": semantic_debt or "PASS",
        "manager_review_retry_count": review_attempts,
        "messages": messages + review_messages,
        "manager_fallback_used": False,
        "failed_stage": "",
        "blocking_report": "",
        "error_message": "\n\n".join(error_parts),
        "forced_forward_debt": (
            append_forced_forward_debt(state, "manager_review", semantic_debt, "global")
            if semantic_debt
            else state.get("forced_forward_debt", [])
        ),
    }


@_with_runtime
def manager_agent(state: AgentState):
    print("---MANAGER: Creating Top-Level RTL Plan---")
    system_prompt = load_prompt("manager.md")
    human_template = "User requirement:\n{user_request}"
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                system_prompt,
            ),
            ("human", human_template),
        ]
    )
    payload = {"user_request": state["user_request"]}
    log_agent_prompt("manager", 1, system_prompt, human_template, payload)
    response = (prompt | llm).invoke(payload)
    write_text_artifact("logs/manager_plan_raw_attempt_1.txt", response.content)

    try:
        plan = parse_manager_plan_response(response.content, state.get("max_manager_tasks", 32))
        print(f"---MANAGER: Planned {len(plan)} tasks.---")
        return _manager_success_update(state, plan, [response])
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"---WARNING: Manager produced invalid plan, attempting repair: {exc}---")
        repair_system_prompt = load_prompt("manager_json_repair.md")
        repair_human_template = """
Original user requirement:
{user_request}

Invalid Manager output:
{invalid_output}

Parser error:
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
            "user_request": state["user_request"],
            "invalid_output": response.content,
            "parser_error": str(exc),
        }
        log_agent_prompt(
            "manager_json_repair",
            1,
            repair_system_prompt,
            repair_human_template,
            repair_payload,
        )
        repair_response = (repair_prompt | llm).invoke(repair_payload)
        write_text_artifact("logs/manager_plan_repair_raw_attempt_1.txt", repair_response.content)
        try:
            plan = parse_manager_plan_response(
                repair_response.content, state.get("max_manager_tasks", 32)
            )
            print(f"---MANAGER: Repaired plan with {len(plan)} tasks.---")
            return _manager_success_update(
                state,
                plan,
                [response, repair_response],
                f"Manager plan repaired after invalid JSON: {exc}",
            )
        except (json.JSONDecodeError, ValueError) as repair_exc:
            exc = repair_exc
        print(f"---ERROR: Manager produced invalid plan: {exc}---")
        if state.get("fail_on_manager_fallback"):
            report = f"Manager planning failed and fallback is disabled: {exc}"
            write_text_artifact("failed_attempts/manager_plan_failed.txt", response.content)
            return {
                "manager_plan": [],
                "run_status": "failed",
                "failed_stage": "manager",
                "blocking_report": report,
                "messages": [response],
                "manager_fallback_used": False,
                "error_message": report,
            }
        fallback_plan = [
            {
                "id": "T1",
                "title": "Implement requested RTL",
                "goal": state["user_request"],
                "user_requirement_trace": state["user_request"],
                "dependencies": "N/A: single recovery implementation task.",
                "required_now": "Implement the complete user-requested RTL in this task.",
                "preserve_from_previous": "N/A: fallback creates the first implementation task.",
                "deferred_scope": "N/A: fallback does not create additional tasks.",
                "interfaces": "DESIGN_CHOICE: derive conventional ports, widths, and handshakes without contradicting the user requirement.",
                "parameters": "DESIGN_CHOICE: derive configurable widths/depths and defaults when the user leaves them open.",
                "control_logic": "DESIGN_CHOICE: identify FSMs, enables, valid/ready, done/error, and sequencing needed by the requested behavior.",
                "datapath": "DESIGN_CHOICE: identify registers, counters, arithmetic, memories/FIFOs, muxes, and width policy needed by the requested behavior.",
                "state_registers": "DESIGN_CHOICE: identify state and datapath registers with intentional reset behavior.",
                "reset_clocking": "ASSUMPTION: derive clock/reset details from explicit requirement language and record any reversible convention in Architecture.",
                "behavior": state["user_request"],
                "edge_cases": "DESIGN_CHOICE: identify applicable boundary values, simultaneous events, overflow/underflow, invalid inputs, and backpressure without inventing external requirements.",
                "acceptance_criteria": "Generated RTL must satisfy the original user requirement and pass sanity, lint when available, microarchitecture review, and verification review.",
                "deliverable": "Complete synthesizable Verilog-2001 RTL using .v/.vh files only.",
                "notes": "Fallback plan created because Manager output was not valid structured JSON.",
            }
        ]
        write_json_artifact("manager_plan.json", fallback_plan)
        return {
            "manager_plan": fallback_plan,
            "current_task_index": 0,
            "manager_review_passed": False,
            "manager_review_report": "Manager JSON fallback was used without semantic approval.",
            "manager_review_retry_count": 0,
            "messages": [response],
            "manager_fallback_used": True,
            "failed_stage": "",
            "blocking_report": "",
            "error_message": f"Manager plan fallback used: {exc}",
        }
