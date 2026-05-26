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
    review_feedback = ""
    if state.get("architecture_review_report"):
        review_feedback = (
            "\nPrevious architecture review feedback to fix:\n"
            f"{state['architecture_review_report']}"
        )
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the RTL Architect.
Create an architecture contract that all later agents must follow.

Include:
- Proposed top module name and purpose.
- Clock/reset assumptions and reset polarity.
- External interface summary.
- Module decomposition table: module name, responsibility, inputs, outputs, parameters.
- Interface contract table: signal name, direction, width, clock domain, reset value, timing meaning.
- Key internal blocks with explicit control logic and datapath responsibilities.
- Expected FSMs, counters, registers, muxes, comparators, arithmetic units, and handshakes.
- Pipeline/latency/throughput assumptions.
- Clock-domain and reset-domain assumptions.
- Error, saturation, overflow/underflow, invalid input, and backpressure behavior.
- Parameterization policy.
- Coding constraints for synthesizable RTL.
- Architecture traceability matrix mapping user requirements and Manager tasks to architecture decisions.
- Open questions/TBD list. Do not hide unknowns.
- Verification intent, corner cases, and acceptance criteria.

Use these exact Markdown sections:
1. Top-Level Architecture
2. Clock and Reset Contract
3. External Interface Contract
4. Module Decomposition
5. Control Logic Plan
6. Datapath Plan
7. State, Counters, Registers, and Memories
8. Timing, Latency, Throughput, and Handshakes
9. Error and Boundary Behavior
10. Parameterization and Coding Constraints
11. Requirement Traceability
12. Verification Intent and Acceptance Criteria
13. Open Questions and Assumptions

If a category is not relevant to the user's requirement, explicitly mark it N/A and explain why.
Use TBD only for information truly missing from the user requirement, and state how later agents should resolve or preserve it.
Prefer a complete, implementation-ready contract over a brief high-level design.

Return concise Markdown.
""",
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
            "review_feedback": review_feedback,
        }
    )
    write_text_artifact("architecture_contract.md", response.content)
    return {
        "architecture_contract": response.content,
        "architecture_review_passed": False,
        "messages": [response],
    }


@_with_runtime
def architecture_review_agent(state: AgentState):
    print("---ARCHITECTURE REVIEW: Checking architecture contract completeness---")
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Architecture Review Gate.
Check whether the architecture contract is complete enough for Supervisor, Control/Data Path Planner, Coding Team, and Verification Team.

Review against:
- Original user requirement.
- Full Manager handoff.
- Manager task sequence.

Required architecture coverage:
- Top module and module decomposition.
- External interfaces with direction, width, timing meaning, and reset value.
- Clock/reset assumptions and domains.
- Control/data path responsibilities.
- FSM/counter/register/mux/arithmetic/memory resources.
- Latency, throughput, handshakes, backpressure.
- Error/overflow/underflow/invalid input behavior.
- Parameterization policy.
- Requirement-to-architecture traceability.
- Open TBDs clearly listed.
- Verification intent and acceptance criteria.

Pass policy:
- PASS when the contract is implementation-ready for the current user requirement.
- PASS when optional categories are explicitly marked N/A with a reasonable reason.
- PASS when TBDs are non-blocking or describe facts not present in the user requirement.
- FAIL only for blocking gaps that prevent RTL coding, such as missing top/interface/reset/control/datapath decisions for an explicit requirement.
- Do not fail merely because a generic category like backpressure, overflow, CDC, memory, or pipelining is N/A for this design.

Return only raw JSON:
{{
  "pass": true|false,
  "report": "specific blocking missing or weak architecture items to fix; include non-blocking suggestions separately"
}}
""",
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

    passed, report = parse_review_result(
        response.content, "Architecture review output was not valid JSON."
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
            "messages": [response],
        }

    print("---ARCHITECTURE REVIEW: FAIL---")
    return {
        "architecture_review_passed": False,
        "architecture_review_report": report or "Architecture contract is incomplete.",
        "architecture_retry_count": state.get("architecture_retry_count", 0) + 1,
        "messages": [response],
    }
