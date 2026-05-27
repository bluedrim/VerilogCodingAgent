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
def testbench_team_agent(state: AgentState):
    print("---TESTBENCH TEAM: Creating Smoke Testbench---")
    feedback = render_review_feedback(
        state,
        ("testbench",),
        state.get("max_context_chars", 120_000),
    )
    if feedback != "(none)":
        feedback = (
            "Previous testbench generation failure to fix:\n"
            f"{feedback}"
        )
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are the Verilog Testbench Team.
Create a lightweight smoke testbench for the accepted RTL.

Rules:
- The testbench may be non-synthesizable.
- Use Verilog testbench syntax only. Do not use SystemVerilog constructs or .sv files.
- Instantiate the most likely top module from the RTL context.
- Generate clock/reset stimulus when ports indicate clock/reset.
- Drive simple deterministic stimulus and finish the simulation.
- Return only raw JSON, with no markdown fences or surrounding prose.
- Preferred schema:
  [
    {{"filename": "tb_top.v", "content": "complete Verilog testbench file content"}}
  ]
""",
            ),
            (
                "human",
                """
Original user requirement:
{user_request}

Accepted RTL files:
{rtl_context}

Top module candidates, in observed order:
{top_module_candidates}

Previous testbench files to revise, if any:
{previous_testbench_files}

{feedback}
""",
            ),
        ]
    )
    response = (prompt | llm).invoke(
        {
            "user_request": state["user_request"],
            "rtl_context": render_files_for_prompt(
                state.get("final_files", []), state.get("max_context_chars", 120_000)
            ),
            "top_module_candidates": ", ".join(state.get("top_module_candidates", [])) or "(unknown)",
            "previous_testbench_files": render_files_for_prompt(
                state.get("testbench_files", []), state.get("max_context_chars", 120_000)
            ),
            "feedback": feedback,
        }
    )

    try:
        files = parse_generated_files_response(response.content)
        is_valid, validation_error = validate_generated_files(
            files,
            state.get("max_generated_file_bytes", 500_000),
            state.get("max_generated_files", 64),
        )
        if not is_valid:
            raise ValueError(validation_error)
        write_json_artifact("logs/testbench_files.json", files)
        return {
            "testbench_files": files,
            "generation_ok": True,
            "failed_stage": "",
            "blocking_report": "",
            "messages": [response],
        }
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"---ERROR: Testbench team produced invalid JSON: {exc}---")
        write_text_artifact(
            f"failed_attempts/testbench_invalid_json_attempt_{state.get('testbench_retry_count', 0) + 1}.txt",
            response.content,
        )
        report = f"Testbench generation failed: {exc}"
        return {
            "testbench_files": [],
            "generation_ok": False,
            "verification_report": report,
            "testbench_retry_count": state.get("testbench_retry_count", 0) + 1,
            "failed_stage": "testbench",
            "blocking_report": report,
            "review_feedback_log": append_review_feedback(state, "testbench", report, "testbench"),
            "messages": [response],
        }


@_with_runtime
def final_lint_agent(state: AgentState):
    print("---FINAL LINT: Checking RTL and Testbench Together---")
    all_files = state.get("final_files", []) + state.get("testbench_files", [])
    lint_result = run_syntax_lint(
        all_files,
        state.get("require_lint", False),
        state.get("lint_timeout_seconds", 30),
    )
    write_text_artifact("logs/final_lint_report.txt", lint_result["report"])
    if lint_result["passed"]:
        return {
            "final_lint_passed": True,
            "final_lint_report": lint_result["report"],
            "failed_stage": "",
            "blocking_report": "",
        }
    return {
        "final_lint_passed": False,
        "final_lint_report": lint_result["report"],
        "failed_stage": "final_lint",
        "blocking_report": lint_result["report"],
    }
