from __future__ import annotations

from .runtime import refresh_globals


def _with_runtime(fn):
    def wrapped(*args, **kwargs):
        refresh_globals(globals())
        return fn(*args, **kwargs)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


def _testbench_status_message(detail: str) -> AIMessage:
    return AIMessage(
        content=(
            f"testbench generation: {detail} "
            "Generated testbench code is carried only in structured file fields and artifacts."
        )
    )


@_with_runtime
def testbench_team_agent(state: AgentState):
    print("---TESTBENCH TEAM: Creating Smoke Testbench---")
    feedback = render_review_feedback(
        state,
        ("testbench", "final_lint"),
        state.get("max_context_chars", 120_000),
    )
    if feedback != "(none)":
        feedback = (
            "Previous testbench generation failure to fix:\n"
            f"{feedback}"
        )
    revision_mode = render_revision_mode(
        state, ("testbench", "final_lint"), "testbench files", "testbench_retry_count"
    )
    revision_checklist = render_revision_checklist(
        state,
        ("testbench", "final_lint"),
        "testbench files",
        state.get("max_context_chars", 120_000),
    )
    system_prompt = load_prompt("testbench.md")
    human_template = """
Original user requirement:
{user_request}

Accepted RTL files:
{rtl_context}

Top module candidates, in observed order:
{top_module_candidates}

Testbench revision mode:
{revision_mode}

Previous testbench files to revise, if any:
{previous_testbench_files}

Testbench revision checklist:
{revision_checklist}

{feedback}
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
        "rtl_context": render_files_for_prompt(
            state.get("final_files", []), state.get("max_context_chars", 120_000)
        ),
        "top_module_candidates": ", ".join(state.get("top_module_candidates", [])) or "(unknown)",
        "previous_testbench_files": render_files_for_prompt(
            state.get("testbench_files", []), state.get("max_context_chars", 120_000)
        ),
        "revision_mode": revision_mode,
        "revision_checklist": revision_checklist,
        "feedback": feedback,
    }
    log_agent_prompt(
        "testbench",
        state.get("testbench_retry_count", 0) + 1,
        system_prompt,
        human_template,
        payload,
    )
    response = (prompt | llm).invoke(payload)
    attempt = state.get("testbench_retry_count", 0) + 1
    write_text_artifact(
        f"logs/testbench_raw_attempt_{attempt}.txt",
        response.content,
    )
    write_text_artifact("logs/testbench_revision_checklist.md", revision_checklist)

    try:
        files = parse_generated_files_response(response.content)
        is_valid, validation_error = validate_generated_files(
            files,
            state.get("max_generated_file_bytes", 500_000),
            state.get("max_generated_files", 64),
        )
        if not is_valid:
            raise ValueError(validation_error)
        if review_feedback_entries(state, ("testbench", "final_lint")) and state.get("testbench_files"):
            previous_names = {
                str(file_info.get("filename", "")).strip()
                for file_info in state.get("testbench_files", [])
            }
            current_names = {
                str(file_info.get("filename", "")).strip()
                for file_info in files
            }
            missing = sorted(name for name in previous_names if name and name not in current_names)
            unchanged = generated_files_fingerprint(files) == generated_files_fingerprint(
                state.get("testbench_files", [])
            )
            if missing or unchanged:
                if missing:
                    report = (
                        "Testbench revision did not return every previous testbench file. "
                        f"Missing files: {', '.join(missing)}"
                    )
                else:
                    report = (
                        "Testbench revision returned unchanged files after failure feedback. "
                        "The testbench must be modified to address the previous failure."
                    )
                write_text_artifact(
                    f"failed_attempts/testbench_unchanged_after_review_attempt_{state.get('testbench_retry_count', 0) + 1}.txt",
                    render_files(files) + "\n\n" + report,
                )
                return {
                    "generation_ok": False,
                    "verification_report": report,
                    "testbench_retry_count": state.get("testbench_retry_count", 0) + 1,
                    "failed_stage": "testbench",
                    "blocking_report": report,
                    "review_feedback_log": append_review_feedback(
                        state, "testbench", report, "testbench"
                    ),
                    "messages": [
                        _testbench_status_message(
                            f"unchanged revision rejected; raw response saved to logs/testbench_raw_attempt_{attempt}.txt"
                        )
                    ],
                }
        write_json_artifact("logs/testbench_files.json", files)
        return {
            "testbench_files": files,
            "generation_ok": True,
            "failed_stage": "",
            "blocking_report": "",
            "messages": [
                _testbench_status_message(
                    f"parsed {len(files)} file(s); raw response saved to logs/testbench_raw_attempt_{attempt}.txt"
                )
            ],
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
            "messages": [
                _testbench_status_message(
                    f"invalid output rejected; raw response saved to failed_attempts/ and logs/testbench_raw_attempt_{attempt}.txt"
                )
            ],
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
            "final_lint_forced_forward": False,
            "final_lint_report": lint_result["report"],
            "failed_stage": "",
            "blocking_report": "",
        }
    next_retry_count = state.get("testbench_retry_count", 0) + 1
    force_forward_after = state.get("max_testbench_retries", 10)
    force_forward = bool(force_forward_after and next_retry_count >= force_forward_after)
    report = lint_result["report"]
    if force_forward:
        print("---FINAL LINT: FORCE-FORWARD THRESHOLD REACHED---")
        report = (
            "FORCED_FORWARD: Final lint reached the force-forward threshold. "
            "Proceeding to final review with the best available RTL/testbench files.\n\n"
            + report
        )
    return {
        "final_lint_passed": False,
        "final_lint_forced_forward": force_forward,
        "final_lint_report": report,
        "generation_ok": False,
        "testbench_retry_count": next_retry_count,
        "failed_stage": "" if force_forward else "final_lint",
        "blocking_report": "" if force_forward else report,
        "review_feedback_log": append_review_feedback(
            state, "final_lint", report, "testbench"
        ),
    }
