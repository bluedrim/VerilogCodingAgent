import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from verilog_agent import coding_team, testbench_team


class ReviewerContractTests(unittest.TestCase):
    REVIEW_PROMPTS = (
        "architecture_review.md",
        "supervisor_review.md",
        "control_datapath_review.md",
        "microarchitecture_review.md",
        "verification.md",
        "verilog_coding_quality_review.md",
        "verilog_coding_closure_review.md",
        "manager_review.md",
    )

    def test_reviewer_prompts_use_shared_valid_boolean_examples(self):
        contract = main.load_prompt("reviewer_contract.md")
        self.assertNotIn("true|false", contract)
        self.assertIn('"blocking_findings": []', contract)
        for filename in self.REVIEW_PROMPTS:
            rendered = main.load_reviewer_prompt(filename)
            self.assertNotIn("true|false", rendered, filename)
            self.assertIn("Shared contract for every review agent", rendered)

    def test_explicit_json_verdict_is_not_overridden_by_report_words(self):
        passed, report, details = main.parse_review_result_with_details(
            json.dumps(
                {
                    "pass": False,
                    "summary": "The packet sounds implementation-ready but has one blocker.",
                    "blocking_findings": [],
                    "warnings": [],
                }
            ),
            "invalid",
        )
        self.assertFalse(passed)
        self.assertTrue(details["json_parsed"])
        self.assertIn("implementation-ready", report)

    def test_structured_findings_are_rendered_for_repair(self):
        passed, report, _details = main.parse_review_result_with_details(
            json.dumps(
                {
                    "pass": False,
                    "summary": "Reset defect.",
                    "blocking_findings": [
                        {
                            "id": "VER-001",
                            "owner": "coding",
                            "target": "counter.v:count",
                            "evidence": "Reset branch does not assign count.",
                            "required_fix": "Assign count on reset.",
                            "acceptance": "Reset edge clears count.",
                        }
                    ],
                    "warnings": [],
                }
            ),
            "invalid",
        )
        self.assertFalse(passed)
        self.assertIn("VER-001", report)
        self.assertIn("required_fix: Assign count on reset.", report)
        self.assertIn("acceptance: Reset edge clears count.", report)


class ManagerContractTests(unittest.TestCase):
    def valid_task(self):
        return {
            "id": "T1",
            "title": "Implement counter RTL",
            "goal": "Implement the complete counter.",
            "deliverable": "A lintable Verilog-2001 module.",
            "user_requirement_trace": "4-bit counter",
            "dependencies": "None",
            "required_now": "Complete counter behavior.",
            "preserve_from_previous": "N/A: first task.",
            "deferred_scope": "N/A: no later task.",
            "interfaces": "DESIGN_CHOICE: conventional names.",
            "behavior": "Increment when enabled.",
            "reset_clocking": "Synchronous active-high reset.",
            "acceptance_criteria": "RTL lints and reset/increment/hold behavior is observable.",
        }

    def test_manager_plan_requires_implementation_handoff_fields(self):
        valid, error = main.validate_plan([self.valid_task()])
        self.assertTrue(valid, error)
        incomplete = self.valid_task()
        incomplete.pop("required_now")
        valid, error = main.validate_plan([incomplete])
        self.assertFalse(valid)
        self.assertIn("required_now", error)

    def test_manager_plan_rejects_bare_tbd(self):
        task = self.valid_task()
        task["interfaces"] = "TBD"
        valid, error = main.validate_plan([task])
        self.assertFalse(valid)
        self.assertIn("ambiguous bare TBD", error)

    def test_current_handoff_does_not_repeat_full_plan(self):
        first = self.valid_task()
        second = dict(first, id="T2", title="Implement output register")
        state = {
            "manager_plan": [first, second],
            "current_task_index": 0,
            "user_request": "Build a counter.",
        }
        handoff = main.current_manager_handoff(state)
        self.assertIn("T1", handoff)
        self.assertNotIn("T2", handoff)
        self.assertNotIn("Full ordered Manager plan", handoff)


class RetryAndDebtTests(unittest.TestCase):
    def test_generation_preflight_moves_to_review_at_retry_limit(self):
        state = {
            "failed_stage": "architecture_generation",
            "architecture_retry_count": 3,
            "max_architecture_retries": 3,
        }
        self.assertEqual(main.architecture_generation_condition(state), "review")

    def test_forced_forward_debt_is_deduplicated_and_kept_in_coding_backlog(self):
        state = {"forced_forward_debt": [], "review_feedback_log": []}
        debt = main.append_forced_forward_debt(
            state, "verification", "VER-001 reset remains broken", "T1"
        )
        state["forced_forward_debt"] = debt
        debt = main.append_forced_forward_debt(
            state, "verification", "VER-001 reset remains broken", "T1"
        )
        self.assertEqual(len(debt), 1)
        state["forced_forward_debt"] = debt
        backlog = main.render_coding_repair_backlog(state)
        self.assertIn("VER-001", backlog)

    def test_upstream_owned_finding_is_not_sent_as_coding_backlog(self):
        state = {
            "forced_forward_debt": [],
            "review_feedback_log": [
                {
                    "stage": "verification",
                    "task_id": "T1",
                    "report": "blocking[1]\nid: VER-002\nowner: architecture\nrequired_fix: resolve contract",
                }
            ],
        }
        self.assertEqual(main.render_coding_repair_backlog(state), "(none)")

    def test_historical_unchanged_hash_failure_is_not_a_coding_obligation(self):
        state = {
            "forced_forward_debt": [],
            "review_feedback_log": [
                {
                    "stage": "coding_unchanged",
                    "task_id": "T1",
                    "report": "owner: coding\nCandidate hash was unchanged.",
                }
            ],
        }
        self.assertEqual(main.render_coding_repair_backlog(state), "(none)")


class ContextRenderingTests(unittest.TestCase):
    def test_clip_text_retains_beginning_and_end(self):
        source = "BEGIN\n" + ("middle\n" * 200) + "END\n"
        clipped = main.clip_text(source, 180)
        self.assertLessEqual(len(clipped), 180)
        self.assertTrue(clipped.startswith("BEGIN"))
        self.assertTrue(clipped.endswith("END\n"))
        self.assertIn("TRUNCATED MIDDLE", clipped)

    def test_file_prompt_retains_manifest_and_each_file_ending(self):
        files = [
            {
                "filename": f"block_{index}.v",
                "content": (
                    f"module block_{index};\n"
                    + (f"wire filler_{index};\n" * 80)
                    + "endmodule\n"
                ),
            }
            for index in range(3)
        ]
        rendered = main.render_files_for_prompt(files, 1500)
        self.assertLessEqual(len(rendered), 1500)
        for index in range(3):
            self.assertIn(f"- block_{index}.v:", rendered)
            self.assertIn(f"--- FILE: block_{index}.v ---", rendered)
        self.assertEqual(rendered.count("endmodule"), 3)


class PromptFileTests(unittest.TestCase):
    def test_coding_prompt_has_one_output_format(self):
        prompt = main.load_prompt("verilog_coding.md")
        self.assertIn("Use exactly this FILE block format", prompt)
        self.assertNotIn("Alternative raw JSON", prompt)

    def test_coding_agents_share_source_authority_contract(self):
        prompt = coding_team._load_coding_prompt("verilog_coding.md")
        self.assertIn("Source authority, from highest to lowest", prompt)
        self.assertIn("original explicit user requirement", prompt)
        self.assertIn("never by character count", prompt)

    def test_reviewer_roles_have_distinct_ownership(self):
        quality = main.load_prompt("verilog_coding_quality_review.md")
        micro = main.load_prompt("microarchitecture_review.md")
        verification = main.load_prompt("verification.md")
        self.assertIn("code-local", quality)
        self.assertIn("structural omissions", micro)
        self.assertIn("externally observable", verification)
        self.assertIn("Do not duplicate internal structure", verification)

    def test_all_prompt_files_are_nonempty(self):
        for path in Path(main.PROMPT_DIR).glob("*.md"):
            self.assertTrue(path.read_text(encoding="utf-8").strip(), path.name)


class SimulationOptionTests(unittest.TestCase):
    def test_simulation_option_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            args = main.build_arg_parser().parse_args([])
        self.assertFalse(args.run_simulation)
        args = main.build_arg_parser().parse_args(["--run-simulation"])
        self.assertTrue(args.run_simulation)

    def test_final_lint_does_not_execute_simulation_when_disabled(self):
        state = {
            "final_files": [{"filename": "top.v", "content": "module top; endmodule"}],
            "testbench_files": [
                {"filename": "tb_top.v", "content": "module tb_top; initial $display(\"TEST_PASS\"); endmodule"}
            ],
            "require_lint": False,
            "run_simulation": False,
            "lint_timeout_seconds": 30,
        }
        with (
            patch.object(main, "run_syntax_lint", return_value={"passed": True, "report": "lint PASS"}),
            patch.object(main, "run_testbench_simulation") as simulation,
            patch.object(main, "write_text_artifact"),
        ):
            result = testbench_team.final_lint_agent(state)
        simulation.assert_not_called()
        self.assertTrue(result["final_lint_passed"])
        self.assertIn("Simulation disabled", result["final_lint_report"])


if __name__ == "__main__":
    unittest.main()
