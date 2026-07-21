import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

import dashboard
import main


class ContinuationRoutingTests(unittest.TestCase):
    def setUp(self):
        self.base_state = {
            "messages": [],
            "manager_plan": [{"id": "T1"}],
            "current_task_index": 0,
        }

    def test_interrupted_generation_retries_same_stage(self):
        state = {**self.base_state, "failed_stage": "architecture_generation"}
        self.assertEqual(main.checkpoint_resume_target("architecture", state), "architecture")

    def test_accepted_task_advances_to_next_task(self):
        state = {**self.base_state, "current_task_index": 0, "skip_testbench": False}
        self.assertEqual(main.checkpoint_resume_target("supervisor_accept", state), "supervisor")

    def test_failed_summary_routes_back_to_repair_owner(self):
        state = {
            **self.base_state,
            "run_status": "failed",
            "failed_stage": "verification",
            "human_approved": False,
        }
        self.assertEqual(
            main.checkpoint_resume_target("summary", state),
            "verilog_coding_team",
        )

    def test_completed_run_has_no_resume_target(self):
        state = {
            **self.base_state,
            "current_task_index": 1,
            "run_status": "passed",
            "failed_stage": "final_review",
            "human_approved": True,
            "writer_errors": [],
        }
        self.assertEqual(main.checkpoint_resume_target("summary", state), "")

    def test_final_lint_failure_resumes_final_lint_when_testbench_is_disabled(self):
        state = {
            **self.base_state,
            "failed_stage": "final_lint",
            "skip_testbench": True,
        }
        self.assertEqual(
            main.checkpoint_resume_target("summary", state),
            "final_lint",
        )


class CheckpointPersistenceTests(unittest.TestCase):
    def test_messages_round_trip_through_checkpoint(self):
        with tempfile.TemporaryDirectory(prefix="verilog_checkpoint_") as tmp_dir:
            previous_artifact_dir = main.ARTIFACT_DIR
            main.ARTIFACT_DIR = Path(tmp_dir)
            try:
                state = {
                    "messages": [
                        HumanMessage(content="counter requirement"),
                        AIMessage(content="counter plan"),
                    ],
                    "user_request": "Build a counter",
                }
                main.write_run_checkpoint(state, "supervisor", phase="before")
                restored, payload = main.read_run_checkpoint(Path(tmp_dir))
            finally:
                main.ARTIFACT_DIR = previous_artifact_dir

        self.assertEqual(payload["resume_stage"], "supervisor")
        self.assertEqual(
            [message.content for message in restored["messages"]],
            ["counter requirement", "counter plan"],
        )

    def test_continuation_restores_original_execution_settings(self):
        args = main.build_arg_parser().parse_args(["--continue", "--artifact-dir", "output_x"])
        execution = {
            "auto_approve": True,
            "skip_testbench": True,
            "require_lint": True,
            "run_simulation": True,
            "max_context_chars": 64000,
            "retry_limits": {
                "manager": 4,
                "architecture": 5,
                "supervisor": 6,
                "control_datapath": 7,
                "coding": 8,
                "testbench": 9,
            },
            "llm_config": {
                "provider": "gpt-oss",
                "model": "custom-rtl-model",
                "temperature": 0.2,
                "api_url": "http://abc.net:30001/chat/completions",
                "timeout_seconds": 240,
                "max_tokens": 16000,
            },
        }

        main.apply_continuation_settings(
            args,
            execution,
            ["--continue", "--artifact-dir", "output_x"],
        )

        self.assertTrue(args.auto_approve)
        self.assertTrue(args.no_testbench)
        self.assertTrue(args.require_lint)
        self.assertTrue(args.run_simulation)
        self.assertEqual(args.max_retries, 8)
        self.assertEqual(args.max_manager_retries, 4)
        self.assertEqual(args.max_testbench_retries, 9)
        self.assertEqual(args.max_context_chars, 64000)
        self.assertEqual(args.llm_provider, "gpt-oss")
        self.assertEqual(args.llm_model, "custom-rtl-model")
        self.assertEqual(args.llm_api_url, "http://abc.net:30001/chat/completions")
        self.assertEqual(args.llm_max_tokens, 16000)

    def test_explicit_continue_provider_does_not_reuse_incompatible_model(self):
        args = main.build_arg_parser().parse_args(
            [
                "--continue",
                "--artifact-dir",
                "output_x",
                "--llm-provider",
                "openai",
            ]
        )
        execution = {
            "llm_config": {
                "provider": "gpt-oss",
                "model": "gpt-oss:20b",
                "api_url": "http://old-endpoint/chat/completions",
            }
        }

        main.apply_continuation_settings(
            args,
            execution,
            [
                "--continue",
                "--artifact-dir",
                "output_x",
                "--llm-provider",
                "openai",
            ],
        )

        self.assertEqual(args.llm_provider, "openai")
        self.assertIsNone(args.llm_model)
        self.assertIsNone(args.llm_api_url)


class DashboardContinuationTests(unittest.TestCase):
    def test_dashboard_html_is_loaded_from_external_file(self):
        self.assertEqual(dashboard.DASHBOARD_HTML_PATH.name, "dashboard.html")
        self.assertNotIn("<!doctype html>", Path(dashboard.__file__).read_text(encoding="utf-8"))
        self.assertIn("Verilog Agent Dashboard", dashboard.load_dashboard_html())

    def test_dashboard_html_has_separate_artifact_and_failed_previews(self):
        html = dashboard.load_dashboard_html()
        self.assertIn('id="artifactList" class="scroll-list"', html)
        self.assertIn('id="failedList" class="scroll-list"', html)
        self.assertIn('id="filePreview" class="preview-pane"', html)
        self.assertIn('id="failedPreview" class="preview-pane"', html)
        self.assertIn(".preview-pane", html)
        self.assertIn("max-height: 520px;", html)
        self.assertIn('data-preview="${escapeHtml(previewTarget)}"', html)

    def test_dashboard_html_places_continue_next_to_start(self):
        html = dashboard.load_dashboard_html()
        toolbar = html.split('<div class="toolbar">', 1)[1].split("</div>", 1)[0]
        launch_actions = html.split('<div class="launch-actions">', 1)[1].split("</div>", 1)[0]
        self.assertNotIn("continueBtn", toolbar)
        self.assertIn("Start new task", launch_actions)
        self.assertIn("Continue task", launch_actions)
        self.assertIn("Stop task", launch_actions)
        self.assertLess(launch_actions.index("startBtn"), launch_actions.index("continueBtn"))
        self.assertLess(launch_actions.index("continueBtn"), launch_actions.index("stopBtn"))

    def test_dashboard_html_can_stop_selected_run(self):
        html = dashboard.load_dashboard_html()
        self.assertIn('id="stopBtn" class="danger"', html)
        self.assertIn('stopBtn.addEventListener("click", stopRun)', html)
        self.assertIn('postJson("/api/stop"', html)

    def test_dashboard_html_preserves_stage_report_formatting(self):
        html = dashboard.load_dashboard_html()
        self.assertIn("white-space: pre-wrap;", html)
        self.assertIn("function formatReportText", html)
        self.assertIn('class="report-body"', html)
        self.assertNotIn("<p>${escapeHtml(text || \"-\")}</p>", html)

    def test_dashboard_html_omits_main_console_output(self):
        html = dashboard.load_dashboard_html()
        self.assertNotIn("main.py Console Output", html)
        self.assertNotIn('id="consoleOutput"', html)
        self.assertNotIn('id="consoleStatus"', html)
        self.assertNotIn("function renderConsole(run)", html)
        self.assertNotIn("run.console_output", html)

    def test_pid_probe_falls_back_when_wnohang_is_unavailable(self):
        with (
            patch.object(dashboard.os, "WNOHANG", None),
            patch.object(dashboard.os, "waitpid") as waitpid,
            patch.object(dashboard.os, "kill", return_value=None) as kill,
        ):
            self.assertTrue(dashboard.pid_is_running(12345))

        waitpid.assert_not_called()
        kill.assert_called_once_with(12345, 0)

    def test_active_probe_checks_job_pid_after_stale_heartbeat_pid(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_pid_") as tmp_dir:
            run_dir = Path(tmp_dir)
            (run_dir / "dashboard_job.json").write_text(
                json.dumps({"pid": 222}),
                encoding="utf-8",
            )
            heartbeat = {"process_id": 111}
            with patch.object(
                dashboard,
                "pid_is_running",
                side_effect=lambda pid: int(pid) == 222,
            ):
                active = dashboard.run_process_is_active(run_dir, heartbeat)

        self.assertTrue(active)

    def test_stale_running_project_is_displayed_as_stopped(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_stale_") as tmp_dir:
            run_dir = Path(tmp_dir)
            (run_dir / "execution_config.json").write_text("{}", encoding="utf-8")
            with patch.object(dashboard, "run_process_is_active", return_value=False):
                status = dashboard.run_status(
                    run_dir,
                    {"run_status": "running"},
                    {"process_id": 999},
                )

        self.assertEqual(status, ("stopped", "stopped", False))

    def test_dashboard_treats_client_disconnects_as_nonfatal(self):
        aborted = ConnectionAbortedError("client aborted")
        win_abort = OSError("windows client aborted")
        win_abort.winerror = 10053
        self.assertTrue(dashboard.is_client_disconnect(aborted))
        self.assertTrue(dashboard.is_client_disconnect(BrokenPipeError("broken pipe")))
        self.assertTrue(dashboard.is_client_disconnect(ConnectionResetError("reset")))
        self.assertTrue(dashboard.is_client_disconnect(win_abort))
        self.assertFalse(dashboard.is_client_disconnect(OSError("real server error")))

    def test_dashboard_server_suppresses_client_disconnect_tracebacks(self):
        server = dashboard.DashboardServer.__new__(dashboard.DashboardServer)
        aborted = ConnectionAbortedError("client aborted")
        with (
            patch.object(dashboard.sys, "exc_info", return_value=(ConnectionAbortedError, aborted, None)),
            patch.object(dashboard.ThreadingHTTPServer, "handle_error") as parent_handle_error,
        ):
            server.handle_error(None, ("127.0.0.1", 12345))

        parent_handle_error.assert_not_called()

    def test_dashboard_uses_windows_detached_process_flags(self):
        with (
            patch.object(dashboard.os, "name", "nt"),
            patch.object(dashboard.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True),
            patch.object(dashboard.subprocess, "DETACHED_PROCESS", 0x8, create=True),
        ):
            options = dashboard.agent_process_options()

        self.assertEqual(options, {"creationflags": 0x208})

    def test_dashboard_start_launches_agent_as_detached_process(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_start_") as tmp_dir:
            root = Path(tmp_dir)
            (root / "main.py").write_text("print('stub')\n", encoding="utf-8")
            captured = {}

            class FakeProcess:
                pid = 515151

            def fake_popen(command, **kwargs):
                captured["command"] = command
                captured["kwargs"] = kwargs
                return FakeProcess()

            with patch.object(dashboard.subprocess, "Popen", side_effect=fake_popen):
                result = dashboard.start_agent_run(
                    root,
                    {
                        "specText": "Create a counter module",
                        "filename": "counter.md",
                        "llmProvider": "gpt-oss",
                    },
                )

            run_dir = root / result["artifact_dir"]
            job = json.loads((run_dir / "dashboard_job.json").read_text(encoding="utf-8"))

        self.assertEqual(result["pid"], 515151)
        self.assertIn("--spec", captured["command"])
        self.assertIn("--llm-provider", captured["command"])
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertTrue(captured["kwargs"]["close_fds"])
        self.assertEqual(captured["kwargs"]["stdin"], dashboard.subprocess.DEVNULL)
        self.assertEqual(captured["kwargs"]["env"]["PYTHONUNBUFFERED"], "1")
        self.assertEqual(job["options"]["llm_provider"], "gpt-oss")

    def test_dashboard_derives_task_goal_and_progress_from_checkpoint(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_task_") as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "output_counter_20260714_120000"
            run_dir.mkdir()
            (run_dir / "run_state_checkpoint.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "phase": "before",
                        "resume_stage": "supervisor",
                        "state": {
                            "current_task_index": 0,
                            "manager_plan": [
                                {
                                    "id": "T1",
                                    "title": "Counter RTL",
                                    "goal": "Implement enable-controlled counter datapath.",
                                },
                                {
                                    "id": "T2",
                                    "title": "Counter testbench",
                                    "goal": "Verify reset and enable behavior.",
                                },
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            summary = dashboard.build_run_summary(root, run_dir)

        self.assertEqual(summary["manager_task_count"], 2)
        self.assertEqual(summary["current_task_index"], 0)
        self.assertEqual(summary["task_progress_current"], 1)
        self.assertEqual(summary["task_progress_total"], 2)
        self.assertEqual(summary["active_task_id"], "T1")
        self.assertEqual(summary["active_task_title"], "Counter RTL")
        self.assertEqual(summary["active_task_goal"], "Implement enable-controlled counter datapath.")

    def test_dashboard_derives_pipeline_retries_from_checkpoint_and_config(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_retry_") as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "output_counter_20260714_130000"
            run_dir.mkdir()
            (run_dir / "run_state_checkpoint.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "phase": "after",
                        "resume_stage": "verilog_coding_team",
                        "state": {
                            "architecture_retry_count": 2,
                            "coding_retry_count": 4,
                            "verification_retry_count": 1,
                            "max_retries": 10,
                            "max_architecture_retries": 3,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "execution_config.json").write_text(
                json.dumps(
                    {
                        "retry_limits": {
                            "architecture": 3,
                            "coding": 10,
                            "verification": 10,
                        }
                    }
                ),
                encoding="utf-8",
            )

            summary = dashboard.build_run_summary(root, run_dir)

        stages = {stage["id"]: stage for stage in summary["stages"]}
        self.assertEqual(stages["architecture"]["retry_count"], 2)
        self.assertEqual(stages["architecture"]["retry_limit"], 3)
        self.assertEqual(stages["coding"]["retry_count"], 4)
        self.assertEqual(stages["coding"]["retry_limit"], 10)
        self.assertEqual(stages["verification"]["retry_count"], 1)
        self.assertEqual(stages["verification"]["retry_limit"], 10)

    def test_dashboard_pipeline_uses_checkpoint_pass_flags_without_summary(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_checkpoint_flags_") as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "output_counter_20260714_130500"
            run_dir.mkdir()
            (run_dir / "run_state_checkpoint.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "phase": "after",
                        "resume_stage": "verification_team",
                        "state": {
                            "manager_review_passed": True,
                            "architecture_review_passed": True,
                            "supervisor_review_passed": True,
                            "control_datapath_review_passed": True,
                            "generation_ok": True,
                            "microarchitecture_passed": True,
                            "verification_passed": False,
                            "verification_retry_count": 2,
                        },
                    }
                ),
                encoding="utf-8",
            )

            summary = dashboard.build_run_summary(root, run_dir)

        stages = {stage["id"]: stage for stage in summary["stages"]}
        self.assertEqual(stages["manager"]["status"], "PASS")
        self.assertEqual(stages["architecture"]["status"], "PASS")
        self.assertEqual(stages["supervisor"]["status"], "PASS")
        self.assertEqual(stages["control_datapath"]["status"], "PASS")
        self.assertEqual(stages["coding"]["status"], "PASS")
        self.assertEqual(stages["microarchitecture"]["status"], "PASS")
        self.assertEqual(stages["verification"]["status"], "RESUME")
        self.assertEqual(stages["verification"]["retry_count"], 2)

    def test_dashboard_pipeline_separates_testbench_from_final_lint(self):
        checkpoint = {
            "resume_stage": "final_lint",
            "state": {
                "testbench_files": [{"filename": "tb_counter.v", "content": "module tb; endmodule"}],
                "testbench_retry_count": 3,
                "final_lint_passed": False,
            },
        }
        execution = {"retry_limits": {"testbench": 5}}

        stages = {
            stage["id"]: stage
            for stage in dashboard.build_stages(
                {},
                {},
                [],
                checkpoint,
                execution,
                {},
                active_run=True,
            )
        }

        self.assertEqual(stages["testbench"]["status"], "PASS")
        self.assertEqual(stages["testbench"]["retry_count"], 3)
        self.assertEqual(stages["final_lint"]["status"], "ACTIVE")
        self.assertEqual(stages["final_lint"]["retry_count"], 0)

    def test_dashboard_pipeline_does_not_pass_current_stage_from_old_files(self):
        checkpoint = {
            "resume_stage": "testbench_team",
            "state": {
                "testbench_files": [{"filename": "tb_counter.v", "content": "module tb; endmodule"}],
                "testbench_retry_count": 2,
                "failed_stage": "testbench",
            },
        }

        stages = {
            stage["id"]: stage
            for stage in dashboard.build_stages(
                {},
                {},
                [],
                checkpoint,
                {},
                {},
                active_run=False,
            )
        }

        self.assertEqual(stages["testbench"]["status"], "RESUME")
        self.assertEqual(stages["testbench"]["retry_count"], 2)

    def test_dashboard_pipeline_marks_forced_stage_with_warn_class(self):
        checkpoint = {
            "resume_stage": "supervisor",
            "state": {
                "architecture_review_forced_forward": True,
                "architecture_retry_count": 10,
            },
        }

        stages = {
            stage["id"]: stage
            for stage in dashboard.build_stages({}, {}, [], checkpoint, {}, {})
        }

        self.assertEqual(stages["architecture"]["status"], "FORCED")
        self.assertEqual(stages["architecture"]["status_code"], "force")
        self.assertIn('status === "force"', dashboard.load_dashboard_html())

    def test_dashboard_collects_stage_reports_from_checkpoint_state(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_reports_") as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "output_counter_20260714_140000"
            run_dir.mkdir()
            (run_dir / "run_state_checkpoint.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "phase": "after",
                        "resume_stage": "verilog_coding_team",
                        "state": {
                            "architecture_review_report": "Architecture needs reset detail.",
                            "supervisor_review_report": "Supervisor packet is PASS.",
                            "control_datapath_review_report": "Control path lacks enable.",
                            "microarchitecture_report": "FSM/datapath coupling is unclear.",
                            "verification_report": "Lint failed on counter.v.",
                            "final_lint_report": "Final lint not run.",
                            "blocking_report": "Current blocker is verification.",
                        },
                    }
                ),
                encoding="utf-8",
            )

            summary = dashboard.build_run_summary(root, run_dir)

        self.assertEqual(summary["last_reports"]["architecture"], "Architecture needs reset detail.")
        self.assertEqual(summary["last_reports"]["supervisor"], "Supervisor packet is PASS.")
        self.assertEqual(summary["last_reports"]["control_datapath"], "Control path lacks enable.")
        self.assertEqual(summary["last_reports"]["microarchitecture"], "FSM/datapath coupling is unclear.")
        self.assertEqual(summary["last_reports"]["verification"], "Lint failed on counter.v.")
        self.assertEqual(summary["last_reports"]["final_lint"], "Final lint not run.")
        self.assertEqual(summary["last_reports"]["blocking"], "Current blocker is verification.")

    def test_dashboard_displays_runtime_in_minutes(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_runtime_") as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "output_counter_20260714_150000"
            run_dir.mkdir()
            (run_dir / "dashboard_job.json").write_text(
                json.dumps(
                    {
                        "created_at": "2026-07-14T01:00:00+00:00",
                        "started_at": "2026-07-14T01:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "run_state_checkpoint.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "phase": "after",
                        "resume_stage": "verification_team",
                        "saved_at": "2026-07-14T01:02:05+00:00",
                        "state": {},
                    }
                ),
                encoding="utf-8",
            )

            summary = dashboard.build_run_summary(root, run_dir)

        self.assertEqual(summary["runtime_minutes"], 2)
        self.assertEqual(summary["runtime_display"], "2 min")
        self.assertEqual(summary["started_at"], "2026-07-14T01:00:00+00:00")
        self.assertEqual(summary["ended_at"], "2026-07-14T01:02:05+00:00")

    def test_dashboard_summary_omits_main_console_tail(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_console_") as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "output_counter_20260714_151000"
            run_dir.mkdir()
            (run_dir / "dashboard_job.json").write_text(
                json.dumps({"stdout_log": "dashboard_stdout.log"}),
                encoding="utf-8",
            )
            (run_dir / "dashboard_stdout.log").write_text(
                "---MANAGER: Planning---\n---VERILOG CODING TEAM: Implementing T1---\n",
                encoding="utf-8",
            )

            summary = dashboard.build_run_summary(root, run_dir)

        self.assertNotIn("stdout_log", summary)
        self.assertNotIn("console_output", summary)
        self.assertNotIn("console_truncated", summary)
        self.assertNotIn("console_bytes", summary)

    def test_dashboard_stop_marks_project_stopped_and_keeps_continue_checkpoint(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_stop_") as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "output_counter_20260714_152000"
            run_dir.mkdir()
            (run_dir / "dashboard_job.json").write_text(
                json.dumps(
                    {
                        "pid": 515151,
                        "dashboard_status": "running",
                        "stdout_log": "dashboard_stdout.log",
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "dashboard_heartbeat.json").write_text(
                json.dumps({"process_id": 515151}),
                encoding="utf-8",
            )
            (run_dir / "run_state_checkpoint.json").write_text(
                json.dumps({"resume_stage": "verilog_coding_team", "state": {}}),
                encoding="utf-8",
            )

            with patch.object(
                dashboard,
                "terminate_agent_process",
                return_value=(True, "Stop signal sent."),
            ) as terminate:
                result = dashboard.stop_agent_run(root, {"dir": run_dir.name})

            job = json.loads((run_dir / "dashboard_job.json").read_text(encoding="utf-8"))
            heartbeat = json.loads(
                (run_dir / "dashboard_heartbeat.json").read_text(encoding="utf-8")
            )
            summary = dashboard.build_run_summary(root, run_dir)

        terminate.assert_called_once_with(run_dir.resolve(), 515151)
        self.assertEqual(job["dashboard_status"], "stopped")
        self.assertEqual(heartbeat["process_id"], 0)
        self.assertTrue(result["signal_sent"])
        self.assertTrue(result["can_continue"])
        self.assertEqual(result["resume_stage"], "verilog_coding_team")
        self.assertEqual(summary["status"], "stopped")
        self.assertTrue(summary["manually_stopped"])
        self.assertFalse(summary["can_stop"])
        self.assertTrue(summary["can_continue"])

    def test_dashboard_builds_continue_command_for_selected_run(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_") as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "output_counter_20260713_120000"
            run_dir.mkdir()
            (run_dir / "run_state_checkpoint.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "phase": "after",
                        "resume_stage": "verilog_coding_team",
                        "state": {},
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "dashboard_job.json").write_text(
                json.dumps(
                    {
                        "dashboard_status": "stopped",
                        "stopped_at": "2026-07-13T12:10:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            before = dashboard.build_run_summary(root, run_dir)

            captured = {}

            class FakeProcess:
                pid = 424242

            def fake_popen(command, **kwargs):
                captured["command"] = command
                captured["kwargs"] = kwargs
                return FakeProcess()

            with patch.object(dashboard.subprocess, "Popen", side_effect=fake_popen):
                result = dashboard.continue_agent_run(
                    root,
                    {"dir": run_dir.name, "llmProvider": "gpt-oss"},
                )

            job = json.loads((run_dir / "dashboard_job.json").read_text(encoding="utf-8"))

        self.assertTrue(before["can_continue"])
        self.assertEqual(before["resume_stage"], "verilog_coding_team")
        self.assertIn("--continue", captured["command"])
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertTrue(captured["kwargs"]["close_fds"])
        self.assertEqual(result["resume_stage"], "verilog_coding_team")
        self.assertEqual(job["dashboard_status"], "running")
        self.assertEqual(job["stopped_at"], "")
        self.assertEqual(job["options"]["llm_provider"], "gpt-oss")
        self.assertEqual(job["continuation_count"], 1)

    def test_dashboard_default_continue_preserves_saved_options_and_llm(self):
        with tempfile.TemporaryDirectory(prefix="verilog_dashboard_saved_continue_") as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "output_counter_20260713_130000"
            run_dir.mkdir()
            (root / "main.py").write_text("print('stub')\n", encoding="utf-8")
            (run_dir / "run_state_checkpoint.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "phase": "after",
                        "resume_stage": "verification_team",
                        "state": {},
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "execution_config.json").write_text(
                json.dumps(
                    {
                        "auto_approve": False,
                        "skip_testbench": True,
                        "require_lint": True,
                        "max_manager_tasks": 48,
                        "retry_limits": {
                            "manager": 4,
                            "architecture": 5,
                            "supervisor": 6,
                            "control_datapath": 7,
                            "coding": 8,
                            "testbench": 9,
                        },
                        "llm_config": {
                            "provider": "gpt-oss",
                            "model": "saved-model",
                            "api_url": "http://saved/chat/completions",
                        },
                    }
                ),
                encoding="utf-8",
            )
            captured = {}

            class FakeProcess:
                pid = 434343

            def fake_popen(command, **kwargs):
                captured["command"] = command
                return FakeProcess()

            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(dashboard.subprocess, "Popen", side_effect=fake_popen),
            ):
                result = dashboard.continue_agent_run(
                    root,
                    {"dir": run_dir.name, "llmProvider": ""},
                )

            job = json.loads((run_dir / "dashboard_job.json").read_text(encoding="utf-8"))

        command = captured["command"]
        self.assertNotIn("--llm-provider", command)
        self.assertNotIn("--auto-approve", command)
        self.assertIn("--no-auto-approve", command)
        self.assertIn("--no-testbench", command)
        self.assertIn("--require-lint", command)
        self.assertIn("--no-run-simulation", command)
        self.assertEqual(command[command.index("--max-retries") + 1], "8")
        self.assertEqual(command[command.index("--max-manager-retries") + 1], "4")
        self.assertEqual(command[command.index("--max-testbench-retries") + 1], "9")
        self.assertEqual(command[command.index("--max-manager-tasks") + 1], "48")
        self.assertEqual(result["resume_stage"], "verification_team")
        self.assertEqual(job["options"]["llm_provider"], "gpt-oss")
        self.assertFalse(job["options"]["llm_provider_overridden"])


if __name__ == "__main__":
    unittest.main()
