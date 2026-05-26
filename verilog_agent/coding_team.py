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
def verilog_coding_team_agent(state: AgentState):
    task = current_manager_task(state)
    task_id = sanitize_artifact_name(task.get("id"), "task")
    task_label = str(task.get("id") or task_id)
    print(f"---VERILOG CODING TEAM: Implementing {task_label}---")
    feedback = render_review_feedback(
        state,
        (
            "control_datapath_review",
            "microarchitecture_review",
            "verification",
            "verification_lint",
            "coding_format",
        ),
        state.get("max_context_chars", 120_000),
    )
    if feedback != "(none)":
        feedback = (
            "\nReviewer and format feedback that must be fixed in this RTL revision:\n"
            f"{feedback}"
        )

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Verilog Coding Team.
Produce synthesizable RTL files for the Supervisor's assignment.

Rules:
- Use Verilog-2001 only. Do not use SystemVerilog.
- Emit only .v source files and optional .vh headers. Do not emit .sv or .svh files.
- Keep all RTL synthesizable unless a file is clearly a header.
- Preserve existing module interfaces unless the Supervisor explicitly requires an extension.
- Implement the Control/Data Path plan faithfully.
- Separate control and datapath clearly in the code:
  - Use distinct next-state/current-state logic for FSMs with reg/wire declarations.
  - Use explicit control signals for enables, mux selects, load/clear, valid/ready, done/error.
  - Keep datapath registers and arithmetic/comparison logic readable and grouped.
  - Avoid mixing unrelated state updates into one opaque always block.
- Use Verilog always blocks only: always @(posedge clk ...), always @(*), assign, reg, and wire.
- Never use SystemVerilog constructs such as logic, always_ff, always_comb, interface, package, typedef, enum, struct, unique, assert, or import.
- Give every registered control and datapath signal an explicit reset or documented reason it does not need one.
- Include meaningful parameters and comments only where they clarify non-obvious logic.
- Return only raw JSON, with no markdown fences or surrounding prose.
- Preferred schema:
  [
    {{"filename": "module_name.v", "content": "complete Verilog-2001 file content"}}
  ]
- Each content value must contain the complete file content.
""",
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

Current RTL files:
{rtl_context}

Previous candidate RTL to revise, if any:
{previous_candidate_rtl}
{feedback}
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
            "supervisor_plan": state.get("supervisor_plan") or "(none)",
            "control_datapath_plan": state.get("control_datapath_plan") or "(none)",
            "rtl_context": clip_text(
                state.get("rtl_context") or "(none)",
                state.get("max_context_chars", 120_000),
            ),
            "previous_candidate_rtl": render_files_for_prompt(
                state.get("candidate_files", []), state.get("max_context_chars", 120_000)
            ),
            "feedback": feedback,
        }
    )

    try:
        files = _load_json(response.content)
        files = normalize_generated_files(files)
        is_valid, validation_error = validate_generated_files(
            files,
            state.get("max_generated_file_bytes", 500_000),
            state.get("max_generated_files", 64),
        )
        if not is_valid:
            raise ValueError(validation_error)
        print(f"---VERILOG CODING TEAM: Generated {len(files)} candidate files.---")
        write_json_artifact(
            f"logs/{task_id}_coding_attempt_{state.get('coding_retry_count', 0) + 1}.json",
            files,
        )
        return {
            "candidate_files": files,
            "generation_ok": True,
            "microarchitecture_passed": False,
            "messages": [response],
            "failed_stage": "",
            "blocking_report": "",
            "error_message": "",
        }
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"---ERROR: Coding team produced invalid JSON: {exc}---")
        write_text_artifact(
            f"failed_attempts/{task_id}_invalid_json_attempt_{state.get('coding_retry_count', 0) + 1}.txt",
            response.content,
        )
        report = f"Coding output format failed: {exc}. Regenerate valid JSON only."
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
    write_text_artifact(
        f"logs/{task_id}_microarchitecture_static_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.txt",
        static_result["report"],
    )

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Microarchitecture Reviewer.
Review only whether the RTL implementation follows the Control/Data Path plan.

Focus:
- Control and datapath are visibly separated.
- FSM/current-state/next-state structure is clear when an FSM is required.
- Control outputs, enables, load/clear, mux selects, valid/ready/done/error are explicit.
- Datapath registers, counters, arithmetic/comparison units, and memories are grouped and readable.
- Reset behavior covers control state and datapath registers.
- Timing, latency, and backpressure assumptions from the plan are reflected in code.
- RTL uses synthesizable Verilog-2001 only, with .v/.vh files and no SystemVerilog constructs.

Do not perform general functional verification here.
Return only raw JSON:
{{
  "pass": true|false,
  "report": "specific control/datapath implementation findings and required fixes"
}}
""",
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
            "candidate_rtl": render_files_for_prompt(
                merged_files, state.get("max_context_chars", 120_000)
            ),
        }
    )

    passed, report = parse_review_result(
        response.content, "Microarchitecture review output was not valid JSON."
    )
    passed = passed and static_result["passed"]

    if not static_result["passed"]:
        report = f"Static microarchitecture scan failed:\n{static_result['report']}\n\n{report}"

    write_text_artifact(
        f"logs/{task_id}_microarchitecture_review_raw_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.txt",
        response.content,
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
    write_text_artifact(
        f"failed_attempts/{task_id}_microarchitecture_failed_attempt_{state.get('microarchitecture_retry_count', 0) + 1}.txt",
        render_files(state.get("candidate_files", [])) + "\n\n" + report,
    )
    return {
        "microarchitecture_passed": False,
        "microarchitecture_report": report or "Microarchitecture review failed.",
        "microarchitecture_retry_count": state.get("microarchitecture_retry_count", 0) + 1,
        "failed_stage": "microarchitecture_review",
        "blocking_report": report or "Microarchitecture review failed.",
        "review_feedback_log": append_review_feedback(
            state,
            "microarchitecture_review",
            report or "Microarchitecture review failed.",
            task_id,
        ),
        "messages": [response],
    }
