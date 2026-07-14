import json
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
        self.assertLess(launch_actions.index("startBtn"), launch_actions.index("continueBtn"))

    def test_dashboard_html_preserves_stage_report_formatting(self):
        html = dashboard.load_dashboard_html()
        self.assertIn("white-space: pre-wrap;", html)
        self.assertIn("function formatReportText", html)
        self.assertIn('class="report-body"', html)
        self.assertNotIn("<p>${escapeHtml(text || \"-\")}</p>", html)

    def test_pid_probe_falls_back_when_wnohang_is_unavailable(self):
        with (
            patch.object(dashboard.os, "WNOHANG", None),
            patch.object(dashboard.os, "waitpid") as waitpid,
            patch.object(dashboard.os, "kill", return_value=None) as kill,
        ):
            self.assertTrue(dashboard.pid_is_running(12345))

        waitpid.assert_not_called()
        kill.assert_called_once_with(12345, 0)

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
        self.assertEqual(job["options"]["llm_provider"], "gpt-oss")
        self.assertEqual(job["continuation_count"], 1)


if __name__ == "__main__":
    unittest.main()
