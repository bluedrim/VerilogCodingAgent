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


@_with_runtime
def manager_agent(state: AgentState):
    print("---MANAGER: Creating Top-Level RTL Plan---")
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                load_prompt("manager.md"),
            ),
            ("human", "User requirement:\n{user_request}"),
        ]
    )
    response = (prompt | llm).invoke({"user_request": state["user_request"]})
    write_text_artifact("logs/manager_plan_raw_attempt_1.txt", response.content)

    try:
        plan = parse_manager_plan_response(response.content, state.get("max_manager_tasks", 32))
        print(f"---MANAGER: Planned {len(plan)} tasks.---")
        write_json_artifact("manager_plan.json", plan)
        return {
            "manager_plan": plan,
            "current_task_index": 0,
            "messages": [response],
            "manager_fallback_used": False,
            "error_message": "",
        }
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"---WARNING: Manager produced invalid plan, attempting repair: {exc}---")
        repair_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    load_prompt("manager_json_repair.md"),
                ),
                (
                    "human",
                    """
Original user requirement:
{user_request}

Invalid Manager output:
{invalid_output}

Parser error:
{parser_error}
""",
                ),
            ]
        )
        repair_response = (repair_prompt | llm).invoke(
            {
                "user_request": state["user_request"],
                "invalid_output": response.content,
                "parser_error": str(exc),
            }
        )
        write_text_artifact("logs/manager_plan_repair_raw_attempt_1.txt", repair_response.content)
        try:
            plan = parse_manager_plan_response(
                repair_response.content, state.get("max_manager_tasks", 32)
            )
            print(f"---MANAGER: Repaired plan with {len(plan)} tasks.---")
            write_json_artifact("manager_plan.json", plan)
            return {
                "manager_plan": plan,
                "current_task_index": 0,
                "messages": [response, repair_response],
                "manager_fallback_used": False,
                "error_message": f"Manager plan repaired after invalid JSON: {exc}",
            }
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
                "dependencies": "TBD: Manager JSON recovery fallback used.",
                "interfaces": "TBD: derive exact ports, widths, and handshakes from the user requirement.",
                "parameters": "TBD: derive configurable widths/depths and defaults from the user requirement.",
                "control_logic": "TBD: identify FSMs, enables, valid/ready, done/error, and sequencing.",
                "datapath": "TBD: identify registers, counters, arithmetic, memories/FIFOs, muxes, and width policy.",
                "state_registers": "TBD: identify state and datapath registers with reset values.",
                "reset_clocking": "TBD: identify clock domains, reset polarity, reset values, and reset release behavior.",
                "behavior": state["user_request"],
                "edge_cases": "TBD: identify boundary values, simultaneous events, overflow/underflow, invalid inputs, and backpressure.",
                "acceptance_criteria": "Generated RTL must satisfy the original user requirement and pass sanity, lint when available, microarchitecture review, and verification review.",
                "deliverable": "Complete synthesizable Verilog-2001 RTL using .v/.vh files only.",
                "notes": "Fallback plan created because Manager output was not valid structured JSON.",
            }
        ]
        write_json_artifact("manager_plan.json", fallback_plan)
        return {
            "manager_plan": fallback_plan,
            "current_task_index": 0,
            "messages": [response],
            "manager_fallback_used": True,
            "error_message": f"Manager plan fallback used: {exc}",
        }
