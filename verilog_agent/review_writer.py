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
def final_review_agent(state: AgentState):
    if os.getenv("AUTO_APPROVE_FINAL", "").strip().lower() in {"1", "true", "yes"}:
        return {
            "human_approved": True,
            "run_status": "passed",
            "failed_stage": "",
            "blocking_report": "",
        }

    all_files = state.get("final_files", []) + state.get("testbench_files", [])
    print("\n" + "-" * 20 + " FINAL REVIEW " + "-" * 20)
    print(f"Top module candidates: {', '.join(state.get('top_module_candidates', [])) or '(unknown)'}")
    print(f"Final lint: {state.get('final_lint_report') or '(not run)'}")
    forced_debt = state.get("forced_forward_debt", [])
    if forced_debt:
        print(f"Open force-forward review debt: {len(forced_debt)} item(s)")
        for item in forced_debt:
            print(
                f"- stage={item.get('stage', 'unknown')} task={item.get('task_id', 'global')}: "
                f"{item.get('report', '')}"
            )
    print(render_files_for_prompt(all_files, state.get("max_context_chars", 120_000)))
    print("\n" + "-" * 54)
    feedback = input("Write files? (approve / reject): ").strip().lower()
    if feedback == "approve":
        write_text_artifact("logs/final_human_approval.txt", "approve")
        return {
            "human_approved": True,
            "run_status": "passed",
            "failed_stage": "",
            "blocking_report": "",
        }

    write_text_artifact("logs/final_human_approval.txt", feedback or "reject")
    return {
        "human_approved": False,
        "run_status": "failed",
        "failed_stage": "final_review",
        "blocking_report": f"Final human approval rejected: {feedback or 'reject'}",
        "verification_report": f"Final human approval rejected: {feedback or 'reject'}",
    }


@_with_runtime
def summary_agent(state: AgentState):
    print("---SUMMARY: Writing Run Summary---")
    writer_errors = collect_writer_errors(state)
    failed_stage = state.get("failed_stage", "")
    run_status = state.get("run_status", "")
    if not run_status:
        if state.get("human_approved"):
            run_status = "passed"
        elif failed_stage:
            run_status = "failed"
        else:
            run_status = "not_written"
    if writer_errors:
        failed_stage = failed_stage or "writer"
        run_status = "failed"
    blocking_report = (
        "\n".join(writer_errors)
        or state.get("blocking_report")
        or state.get("verification_report")
        or state.get("final_lint_report")
        or ""
    )
    retry_counts = {
        "manager": state.get("manager_review_retry_count", 0),
        "architecture": state.get("architecture_retry_count", 0),
        "supervisor": state.get("supervisor_retry_count", 0),
        "control_datapath": state.get("control_datapath_retry_count", 0),
        "coding": state.get("coding_retry_count", 0),
        "microarchitecture": state.get("microarchitecture_retry_count", 0),
        "verification": state.get("verification_retry_count", 0),
        "testbench": state.get("testbench_retry_count", 0),
    }
    retry_limits = {
        "manager": state.get("max_manager_retries", 0),
        "architecture": state.get("max_architecture_retries", 0),
        "supervisor": state.get("max_supervisor_retries", 0),
        "control_datapath": state.get("max_control_datapath_retries", 0),
        "coding": state.get("max_retries", 0),
        "microarchitecture": state.get("max_retries", 0),
        "verification": state.get("max_retries", 0),
        "testbench": state.get("max_testbench_retries", 0),
    }
    manager_plan = state.get("manager_plan", [])
    current_task_index = state.get("current_task_index", 0)
    active_task = (
        manager_plan[current_task_index]
        if isinstance(current_task_index, int) and 0 <= current_task_index < len(manager_plan)
        else {}
    )
    if not isinstance(active_task, dict):
        active_task = {}
    stage_snapshot = {
        "run_status": run_status,
        "failed_stage": failed_stage,
        "blocking_report": blocking_report,
        "current_task_index": current_task_index,
        "manager_task_count": len(manager_plan),
        "active_task_id": active_task.get("id", ""),
        "active_task_title": active_task.get("title", ""),
        "stage_pass_flags": {
            "manager_review": state.get("manager_review_passed", False),
            "architecture_review": state.get("architecture_review_passed", False),
            "architecture_review_forced_forward": state.get(
                "architecture_review_forced_forward", False
            ),
            "supervisor_review": state.get("supervisor_review_passed", False),
            "supervisor_review_forced_forward": state.get(
                "supervisor_review_forced_forward", False
            ),
            "control_datapath_review": state.get("control_datapath_review_passed", False),
            "control_datapath_review_forced_forward": state.get(
                "control_datapath_review_forced_forward", False
            ),
            "coding_generation": state.get("generation_ok", False),
            "coding_review_forced_forward": state.get(
                "coding_review_forced_forward", False
            ),
            "microarchitecture_review": state.get("microarchitecture_passed", False),
            "microarchitecture_review_forced_forward": state.get(
                "microarchitecture_review_forced_forward", False
            ),
            "verification": state.get("verification_passed", False),
            "verification_review_forced_forward": state.get(
                "verification_review_forced_forward", False
            ),
            "final_lint": state.get("final_lint_passed", False),
            "final_lint_forced_forward": state.get("final_lint_forced_forward", False),
            "human_approval": state.get("human_approved", False),
        },
        "retry_counts": retry_counts,
        "retry_limits": retry_limits,
        "stage_retry_limits_enforced": True,
        "review_force_forward_after": {
            "manager": state.get("max_manager_retries", 0),
            "architecture": state.get("max_architecture_retries", 0),
            "supervisor": state.get("max_supervisor_retries", 0),
            "control_datapath": state.get("max_control_datapath_retries", 0),
            "coding_local_gate": state.get("max_retries", 0),
            "microarchitecture": state.get("max_retries", 0),
            "verification": state.get("max_retries", 0),
            "final_lint": state.get("max_testbench_retries", 0),
        },
        "last_reports": {
            "manager": state.get("manager_review_report", ""),
            "architecture": state.get("architecture_review_report", ""),
            "supervisor": state.get("supervisor_review_report", ""),
            "control_datapath": state.get("control_datapath_review_report", ""),
            "microarchitecture": state.get("microarchitecture_report", ""),
            "verification": state.get("verification_report", ""),
            "lint": state.get("lint_report", ""),
            "final_lint": state.get("final_lint_report", ""),
        },
        "recent_feedback_count": len(state.get("review_feedback_log", [])),
    }
    summary = {
        "run_id": state.get("run_id", ""),
        "run_status": run_status,
        "failed_stage": failed_stage,
        "blocking_report": blocking_report,
        "writer_errors": writer_errors,
        "review_feedback_log": state.get("review_feedback_log", []),
        "forced_forward_debt": state.get("forced_forward_debt", []),
        "forced_forward_debt_count": len(state.get("forced_forward_debt", [])),
        "artifact_dir": str(ARTIFACT_DIR),
        "stage_snapshot": stage_snapshot,
        "stage_snapshot_saved": str(ARTIFACT_DIR / "run_progress_snapshot.json"),
        "llm_config": active_llm_config,
        "llm_config_saved": str(ARTIFACT_DIR / "llm_config.json"),
        "user_request_saved": str(ARTIFACT_DIR / "user_requirement.txt"),
        "manager_plan_saved": str(ARTIFACT_DIR / "manager_plan.json"),
        "manager_task_count": len(state.get("manager_plan", [])),
        "manager_fallback_used": state.get("manager_fallback_used", False),
        "fail_on_manager_fallback": state.get("fail_on_manager_fallback", False),
        "accepted_task_count": state.get("current_task_index", 0),
        "pending_task_count": max(
            len(state.get("manager_plan", [])) - state.get("current_task_index", 0),
            0,
        ),
        "architecture_contract_saved": str(ARTIFACT_DIR / "architecture_contract.md"),
        "last_architecture_review_report": state.get("architecture_review_report", ""),
        "architecture_review_forced_forward": state.get(
            "architecture_review_forced_forward", False
        ),
        "last_supervisor_review_report": state.get("supervisor_review_report", ""),
        "supervisor_review_forced_forward": state.get(
            "supervisor_review_forced_forward", False
        ),
        "control_datapath_plans_saved": str(ARTIFACT_DIR / "logs"),
        "control_datapath_review_forced_forward": state.get(
            "control_datapath_review_forced_forward", False
        ),
        "last_microarchitecture_report": state.get("microarchitecture_report", ""),
        "microarchitecture_review_forced_forward": state.get(
            "microarchitecture_review_forced_forward", False
        ),
        "rtl_files": [file_info["filename"] for file_info in state.get("final_files", [])],
        "testbench_files": [
            file_info["filename"] for file_info in state.get("testbench_files", [])
        ],
        "compile_order_saved": str(ARTIFACT_DIR / "compile_order.f"),
        "file_manifest_saved": str(ARTIFACT_DIR / "file_manifest.json"),
        "top_module_candidates": state.get("top_module_candidates", []),
        "human_approved": state.get("human_approved", False),
        "last_verification_report": state.get("verification_report", ""),
        "verification_review_forced_forward": state.get(
            "verification_review_forced_forward", False
        ),
        "coding_review_forced_forward": state.get("coding_review_forced_forward", False),
        "last_lint_report": state.get("lint_report", ""),
        "lint_tool": discover_lint_tool(),
        "require_lint": state.get("require_lint", False),
        "run_simulation": state.get("run_simulation", False),
        "lint_timeout_seconds": state.get("lint_timeout_seconds", 30),
        "allow_blackboxes": state.get("allow_blackboxes", False),
        "max_generated_file_bytes": state.get("max_generated_file_bytes", 0),
        "max_generated_files": state.get("max_generated_files", 0),
        "max_context_chars": state.get("max_context_chars", 0),
        "max_user_request_chars": state.get("max_user_request_chars", 0),
        "max_manager_tasks": state.get("max_manager_tasks", 0),
        "final_lint_passed": state.get("final_lint_passed", False),
        "final_lint_forced_forward": state.get("final_lint_forced_forward", False),
        "final_lint_report": state.get("final_lint_report", ""),
        "retry_counts": retry_counts,
        "retry_limits": retry_limits,
        "stage_retry_limits_enforced": True,
        "review_force_forward_after": {
            "manager": state.get("max_manager_retries", 0),
            "architecture": state.get("max_architecture_retries", 0),
            "supervisor": state.get("max_supervisor_retries", 0),
            "control_datapath": state.get("max_control_datapath_retries", 0),
            "coding_local_gate": state.get("max_retries", 0),
            "microarchitecture": state.get("max_retries", 0),
            "verification": state.get("max_retries", 0),
            "final_lint": state.get("max_testbench_retries", 0),
        },
        "logs_dir": str(ARTIFACT_DIR / "logs"),
        "agent_message_snapshots_dir": str(ARTIFACT_DIR / "logs" / "agent_messages"),
        "failed_attempts_dir": str(ARTIFACT_DIR / "failed_attempts"),
    }
    all_files = state.get("final_files", []) + state.get("testbench_files", [])
    write_compile_order(all_files)
    write_json_artifact("file_manifest.json", build_file_manifest(all_files))
    write_json_artifact("run_progress_snapshot.json", stage_snapshot)
    write_json_artifact("run_summary.json", summary)
    return {}


@_with_runtime
def collect_writer_errors(state: AgentState):
    errors = list(state.get("writer_errors", []))
    for message in state.get("messages", []):
        if not isinstance(message, ToolMessage):
            continue
        content = str(getattr(message, "content", "") or "").strip()
        if content.lower().startswith("error"):
            errors.append(content)
    return errors


@_with_runtime
def writer_agent(state: AgentState):
    print("---WRITER: Writing Final RTL Files---")
    files = state.get("final_files", []) + state.get("testbench_files", [])
    if not files:
        print("---ERROR: No files to write.---")
        return {"writer_errors": ["No files to write."]}

    results = []
    errors = []
    for file_info in files:
        result = write_verilog_file.invoke(
            {
                "filename": file_info["filename"],
                "content": file_info["content"],
            }
        )
        result_text = str(result)
        results.append(result_text)
        print(f"- {result_text}")
        if result_text.lower().startswith("error"):
            errors.append(result_text)
    recovered_writer_failure = not errors and state.get("failed_stage") == "writer"
    return {
        "writer_results": results,
        "writer_errors": errors,
        "failed_stage": "" if recovered_writer_failure else state.get("failed_stage", ""),
        "blocking_report": ""
        if recovered_writer_failure
        else state.get("blocking_report", ""),
    }
