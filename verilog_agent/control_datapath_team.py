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
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Control/Data Path Planner for a Verilog RTL team.
Before coding, create a concrete micro-architecture plan that cleanly separates control logic from datapath.

Include these sections:
1. Control Logic
   - FSM states and transitions, or explain why no FSM is needed.
   - Control outputs, enables, mux selects, valid/ready/done/error behavior.
   - Reset behavior for every control register.
2. Datapath
   - Data registers, counters, accumulators, memories/FIFOs, arithmetic/comparison units.
   - Data movement per cycle and mux/enable conditions.
   - Width/parameter choices and overflow/underflow handling.
3. Timing Contract
   - Latency, throughput, handshake assumptions, and backpressure handling.
4. Coding Guidance
   - Recommended Verilog-2001 always @(*) and always @(posedge clk ...) block structure.
   - Signals that should be separated into next-state, registered-state, control, and datapath groups.
   - Use reg/wire only; do not request SystemVerilog logic, always_ff, always_comb, interface, package, typedef, enum, or struct.
5. Verification Focus
   - Specific corner cases the Verification Team must check for this task.
6. Implementation Checklist
   - Bullet list of concrete code features that must appear in the RTL.
   - Include expected signal names or naming patterns when useful.
   - Include what must be separated into control blocks and datapath blocks.

Return concise Markdown. Do not write RTL code.
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

Existing RTL context:
{rtl_context}

Previous Control/Data Path plan to revise, if any:
{previous_control_datapath_plan}

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
            "review_feedback": review_feedback,
        }
    )
    write_text_artifact(
        f"logs/{task_id}_control_datapath_plan.md",
        response.content,
    )
    return {
        "control_datapath_plan": response.content,
        "control_datapath_review_passed": False,
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
                """
You are the Control/Data Path Review Gate.
Check whether the micro-architecture plan is concrete enough for RTL coding.

Required coverage:
- FSM/state sequencing, or a clear reason no FSM is needed.
- Control outputs, enables, mux selects, valid/ready, done/error, load/clear.
- Datapath registers, counters, arithmetic/comparison units, memories/FIFOs, and muxes.
- Cycle-level timing, latency, throughput, reset release, and backpressure.
- Width and parameter policy, including overflow/underflow handling.
- Clear mapping from Supervisor assignment to implementation checklist.
- Verification focus with concrete corner cases.

Return only raw JSON:
{{
  "pass": true|false,
  "report": "specific missing or weak control/datapath plan items to fix"
}}
""",
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

    passed, report = parse_review_result(
        response.content, "Control/Data Path review output was not valid JSON."
    )

    write_text_artifact(
        f"logs/{task_id}_control_datapath_review_raw_attempt_{state.get('control_datapath_retry_count', 0) + 1}.txt",
        response.content,
    )
    write_text_artifact(
        f"logs/{task_id}_control_datapath_review_attempt_{state.get('control_datapath_retry_count', 0) + 1}.md",
        report or ("PASS" if passed else "FAIL"),
    )

    if passed:
        print("---CONTROL/DATAPATH REVIEW: PASS---")
        return {
            "control_datapath_review_passed": True,
            "control_datapath_review_report": report or "PASS",
            "messages": [response],
        }

    print("---CONTROL/DATAPATH REVIEW: FAIL---")
    return {
        "control_datapath_review_passed": False,
        "control_datapath_review_report": report or "Control/Data Path plan is incomplete.",
        "control_datapath_retry_count": state.get("control_datapath_retry_count", 0) + 1,
        "review_feedback_log": append_review_feedback(
            state,
            "control_datapath_review",
            report or "Control/Data Path plan is incomplete.",
            task_id,
        ),
        "messages": [response],
    }
