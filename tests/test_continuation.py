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
    def test_pid_probe_falls_back_when_wnohang_is_unavailable(self):
        with (
            patch.object(dashboard.os, "WNOHANG", None),
            patch.object(dashboard.os, "waitpid") as waitpid,
            patch.object(dashboard.os, "kill", return_value=None) as kill,
        ):
            self.assertTrue(dashboard.pid_is_running(12345))

        waitpid.assert_not_called()
        kill.assert_called_once_with(12345, 0)

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
        self.assertEqual(result["resume_stage"], "verilog_coding_team")
        self.assertEqual(job["options"]["llm_provider"], "gpt-oss")
        self.assertEqual(job["continuation_count"], 1)


if __name__ == "__main__":
    unittest.main()
