import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage

import main
from verilog_agent import coding_team


class FakePrompt:
    def __or__(self, llm):
        return llm


class CodingPlanningTests(unittest.TestCase):
    def setUp(self):
        coding_team.refresh_globals(coding_team.__dict__)

    def test_quality_contract_contains_cycle_and_numeric_guards(self):
        contract = coding_team._render_rtl_quality_contract(
            {},
            {
                "behavior": "Count while enable is high.",
                "reset_clocking": "Synchronous active-high reset.",
                "acceptance_criteria": "Done pulses at the terminal count.",
            },
            8000,
        )

        self.assertIn("Required behavior: Count while enable is high.", contract)
        self.assertIn("Reset/clocking expectations: Synchronous active-high reset.", contract)
        self.assertIn("arithmetic widths", contract)
        self.assertIn("back-to-back transactions", contract)

    def test_fresh_implementation_always_uses_action_planner(self):
        state = {
            "manager_plan": [{"id": "T1", "title": "Counter"}],
            "current_task_index": 0,
            "candidate_files": [],
            "review_feedback_log": [],
            "coding_retry_count": 0,
            "max_context_chars": 120_000,
        }
        prompt_payload = {
            "user_request": "Build a counter",
            "supervisor_plan": "Implement a counter module.",
            "control_datapath_plan": "Counter register plus enable control.",
            "quality_contract": "Cycle accurate counter contract.",
            "implementation_obligations": "Implement reset and enable.",
            "previous_candidate_rtl": "(none)",
            "coding_repair_backlog": "(none)",
            "revision_plan": "(none)",
            "repair_brief": "(none)",
            "repair_contract": "(none)",
        }
        fake_llm = Mock()
        fake_llm.invoke.return_value = AIMessage(
            content=(
                "Mandatory RTL coding action plan:\n"
                "- Files/modules and interface constraints: counter.v\n"
                "- Behavioral invariants: reset clears count and enable advances it.\n"
                "- Cycle/latency trace: count updates on each enabled rising edge.\n"
                "- State transition and control decisions: enable owns the update.\n"
                "- Register/datapath update table: count <= count + 1.\n"
                "- Pre-return acceptance checks: reset and terminal count."
            )
        )

        with (
            patch.object(coding_team, "llm", fake_llm),
            patch.object(
                coding_team.ChatPromptTemplate,
                "from_messages",
                return_value=FakePrompt(),
            ),
            patch.object(coding_team, "load_prompt", return_value="planner prompt"),
            patch.object(coding_team, "write_text_artifact"),
            patch.object(coding_team, "write_json_artifact"),
            patch.object(coding_team, "log_agent_prompt"),
        ):
            plan = coding_team._build_coding_action_plan(
                state,
                state["manager_plan"][0],
                "T1",
                prompt_payload,
                12_000,
            )

        fake_llm.invoke.assert_called_once()
        self.assertIn("Planner-derived RTL edit plan", plan)
        self.assertIn("Cycle/latency trace", plan)
        self.assertIn("Deterministic implementation guardrails", plan)

    def test_prompt_budget_preserves_payload_that_already_fits(self):
        payload = {
            "user_request": "counter",
            "candidate_rtl": "module counter; endmodule\n",
            "coding_action_plan": "implement reset and enable",
        }

        rendered, report = coding_team._budget_coding_prompt_payload(payload, 10_000)

        self.assertEqual(rendered, payload)
        self.assertFalse(report["truncation_required"])

    def test_prompt_budget_redistributes_space_to_large_rtl(self):
        payload = {
            "user_request": "x",
            "candidate_rtl": "v" * 2_000,
        }

        rendered, report = coding_team._budget_coding_prompt_payload(payload, 1_000)

        initial_candidate_share = report["weighted_payload_budget"] * 30 // 35
        self.assertTrue(report["truncation_required"])
        self.assertGreater(len(rendered["candidate_rtl"]), initial_candidate_share)
        self.assertLessEqual(
            sum(len(value) for value in rendered.values()),
            report["weighted_payload_budget"],
        )


class CodingQualityLoopTests(unittest.TestCase):
    def setUp(self):
        coding_team.refresh_globals(coding_team.__dict__)

    def test_quality_failure_is_repaired_and_reaudited(self):
        state = {
            "coding_retry_count": 0,
            "review_feedback_log": [],
            "candidate_files": [],
            "final_files": [],
        }
        task = {"id": "T1", "title": "Counter"}
        original = [{"filename": "counter.v", "content": "module counter; endmodule"}]
        repaired = [
            {
                "filename": "counter.v",
                "content": "module counter(input clk); always @(posedge clk) begin end endmodule",
            }
        ]
        quality_audit = Mock(side_effect=[(False, "missing sequential behavior"), (True, "")])
        repair_message = AIMessage(content="repair completed")

        with (
            patch.object(coding_team, "_hard_review_gate_report", return_value=""),
            patch.object(coding_team, "_review_gate_report", return_value=""),
            patch.object(coding_team, "_soft_review_scope_report", return_value=""),
            patch.object(coding_team, "_coding_closure_audit", return_value=(True, "")),
            patch.object(coding_team, "_coding_quality_audit", quality_audit),
            patch.object(
                coding_team,
                "_attempt_review_gate_repair",
                return_value=(repaired, repair_message, ""),
            ) as repair,
            patch.object(coding_team, "write_json_artifact"),
        ):
            files, messages, report = coding_team._repair_candidate_against_review_gate(
                state,
                task,
                "T1",
                original,
                {},
                12_000,
                [],
            )

        repair.assert_called_once()
        self.assertEqual(quality_audit.call_count, 2)
        self.assertEqual(files, repaired)
        self.assertEqual(messages, [repair_message])
        self.assertEqual(report, "")

    def test_quality_audit_returns_actionable_failure(self):
        state = {
            "coding_retry_count": 0,
            "final_files": [],
            "lint_timeout_seconds": 30,
            "max_context_chars": 120_000,
        }
        task = {"id": "T1", "title": "Counter"}
        files = [{"filename": "counter.v", "content": "module counter; endmodule"}]
        prompt_payload = {
            "user_request": "Build a counter",
            "architecture_contract": "Use synchronous RTL.",
            "supervisor_plan": "Implement reset and enable.",
            "control_datapath_plan": "Counter register controlled by enable.",
            "quality_contract": "Reset clears count and enable advances it.",
            "coding_action_plan": "Add a clocked counter register.",
            "implementation_obligations": "Provide observable counter behavior.",
        }
        fake_llm = Mock()
        fake_llm.invoke.return_value = AIMessage(
            content=(
                '{"pass": false, "report": '
                '"required_fix: counter.v has no count register or clocked update; '
                'acceptance: count clears on reset and increments on an enabled edge"}'
            )
        )

        with (
            patch.object(coding_team, "llm", fake_llm),
            patch.object(
                coding_team.ChatPromptTemplate,
                "from_messages",
                return_value=FakePrompt(),
            ),
            patch.object(coding_team, "load_prompt", return_value="quality prompt"),
            patch.object(
                coding_team,
                "static_microarchitecture_review",
                return_value={"passed": True, "report": "static pass"},
            ),
            patch.object(
                coding_team,
                "run_syntax_lint",
                return_value={"passed": True, "report": "lint pass"},
            ),
            patch.object(coding_team, "write_text_artifact"),
            patch.object(coding_team, "write_json_artifact"),
            patch.object(coding_team, "log_agent_prompt"),
        ):
            passed, report = coding_team._coding_quality_audit(
                state,
                task,
                "T1",
                files,
                prompt_payload,
                12_000,
                "initial",
            )

        self.assertFalse(passed)
        self.assertIn("required_fix", report)
        self.assertIn("acceptance", report)
        self.assertIn("counter.v", fake_llm.invoke.call_args.args[0]["candidate_rtl"])


if __name__ == "__main__":
    unittest.main()
