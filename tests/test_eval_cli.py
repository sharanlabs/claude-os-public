"""Exercise public commands through subprocesses, including their failure contracts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "bin" / "eval-runner.py"
LINTER = ROOT / "bin" / "golden-lint.py"


class EvaluationCLI(unittest.TestCase):
    def invoke(self, script, *args):
        return subprocess.run([sys.executable, str(script), *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)

    def task(self, directory, checks, task_id="sample", rubric=""):
        path = directory / (task_id + ".md")
        path.write_text(
            f"---\nid: {task_id}\ntype: deterministic\n---\n\n"
            "## Input\nEvaluate the supplied review result.\n\n"
            "## Reference\nThe structured decision must require changes.\n\n"
            f"## Check\n```json\n{checks}\n```\n{rubric}", encoding="utf-8")
        return path

    def suite(self, task_dir, candidate_dir):
        return self.invoke(RUNNER, "run", "--tasks", task_dir,
                           "--candidates", candidate_dir)

    def test_external_directory_is_linted(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.task(directory, json.dumps({"checks": [
                {"name": "decision", "type": "exact", "field": "decision",
                 "value": "needs_changes"}]}))
            result = self.invoke(LINTER, directory)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("1 task(s) scanned", result.stdout)

    def test_external_empty_directory_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.invoke(LINTER, tmp)
            self.assertEqual(result.returncode, 1)
            self.assertIn("no task files", result.stdout.lower())

    def test_malformed_checks_fail_even_with_a_rubric(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.task(directory, "{broken json}", rubric="- subjective: safety — avoid harm\n")
            result = self.invoke(LINTER, directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("malformed", result.stdout.lower())

    def test_non_list_checks_fail_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.task(directory, '{"checks": {"type": "exact", "value": "x"}}',
                      rubric="- subjective: safety — avoid harm\n")
            result = self.invoke(LINTER, directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("checks must be a list", result.stdout.lower())

    def test_unknown_linter_argument_does_not_succeed(self):
        result = self.invoke(LINTER, "--not-a-real-option")
        self.assertNotEqual(result.returncode, 0)

    def test_linter_ignores_input_example_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "sample.md").write_text(
                '---\nid: sample\ntype: deterministic\n---\n\n## Input\n'
                'This is example data, not a grading contract.\n```json\n'
                '[{"type": "exact", "value": "x"}]\n```\n', encoding="utf-8")
            result = self.invoke(LINTER, directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("D6", result.stdout)

    def test_linter_matches_runner_case_insensitive_headings(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self.task(directory, '[{"type": "contains", "value": "Review"}]')
            path.write_text(path.read_text(encoding="utf-8").replace("## Input", "## input")
                            .replace("## Check", "## check"), encoding="utf-8")
            result = self.invoke(LINTER, directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("D1", result.stdout)

    def test_missing_candidate_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            tasks = directory / "tasks"
            candidates = directory / "candidates"
            tasks.mkdir()
            candidates.mkdir()
            self.task(tasks, '[{"type": "exact", "value": "needs_changes"}]')
            result = self.suite(tasks, candidates)
            self.assertEqual(result.returncode, 1)
            self.assertIn("no candidate", result.stdout)

    def test_empty_suite_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.suite(tmp, tmp)
            self.assertEqual(result.returncode, 1)

    def test_runner_reports_malformed_task_and_preserves_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.task(directory, "{broken json}")
            result = self.suite(directory, directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("malformed", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_runner_rejects_non_object_checks_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.task(directory, '["not an object"]')
            (directory / "sample.txt").write_text("needs_changes", encoding="utf-8")
            result = self.suite(directory, directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("each check must be an object", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_zero_checks_never_return_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.task(directory, "[]")
            (directory / "sample.txt").write_text("needs_changes", encoding="utf-8")
            self.assertNotEqual(self.suite(directory, directory).returncode, 0)

    def test_task_id_cannot_read_outside_candidate_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            tasks = directory / "tasks"
            candidates = directory / "candidates"
            tasks.mkdir()
            candidates.mkdir()
            path = self.task(tasks, '[{"type":"exact","value":"SYNTHETIC_OUTSIDE_VALUE"}]')
            (directory / "outside.txt").write_text("SYNTHETIC_OUTSIDE_VALUE", encoding="utf-8")
            for task_id in ("../outside", "..\\outside", "/absolute", "a/b", "a.b"):
                with self.subTest(task_id=task_id):
                    text = path.read_text(encoding="utf-8")
                    start, _, rest = text.partition("\ntype:")
                    path.write_text("---\nid: " + task_id + "\ntype:" + rest, encoding="utf-8")
                    result = self.suite(tasks, candidates)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("unsafe task id", result.stdout.lower())
                    self.assertNotIn("SYNTHETIC_OUTSIDE_VALUE", result.stdout)

    def test_valid_hyphenated_task_id_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.task(directory, '[{"type":"exact","value":"ok"}]', task_id="a-1_VALID")
            (directory / "a-1_VALID.txt").write_text("ok", encoding="utf-8")
            result = self.suite(directory, directory)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_duplicate_task_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self.task(directory, '[{"type":"exact","value":"ok"}]')
            (directory / "second.md").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            (directory / "sample.txt").write_text("ok", encoding="utf-8")
            result = self.suite(directory, directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("duplicate task id", result.stdout.lower())

    def test_candidate_symlink_cannot_read_outside_candidate_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            tasks = directory / "tasks"
            candidates = directory / "candidates"
            tasks.mkdir()
            candidates.mkdir()
            self.task(tasks, '[{"type":"exact","value":"SYNTHETIC_OUTSIDE_VALUE"}]')
            outside = directory / "outside.txt"
            outside.write_text("SYNTHETIC_OUTSIDE_VALUE", encoding="utf-8")
            try:
                (candidates / "sample.txt").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink creation is not available: {exc.__class__.__name__}")
            result = self.suite(tasks, candidates)
            self.assertEqual(result.returncode, 1)
            self.assertIn("outside candidate directory", result.stdout.lower())
            self.assertNotIn("SYNTHETIC_OUTSIDE_VALUE", result.stdout)

    def test_public_positive_and_negative_examples(self):
        example = ROOT / "examples" / "review-contract"
        passing = self.suite(example / "tasks", example / "candidates-good")
        failing = self.suite(example / "tasks", example / "candidates-bad")
        self.assertEqual(passing.returncode, 0, passing.stdout + passing.stderr)
        self.assertEqual(failing.returncode, 1, failing.stdout + failing.stderr)
        self.assertIn("finding-code", failing.stdout)

    def test_public_judge_example_stays_incomplete_until_graded(self):
        example = ROOT / "examples" / "judge"
        result = self.suite(example / "tasks", example / "candidates")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("DEFER", result.stdout)

    def test_public_judge_prompt_contains_full_criteria(self):
        example = ROOT / "examples" / "judge"
        result = self.invoke(RUNNER, "judge-prompt", "--task", example / "tasks" / "002-review-explanation.md",
                             "--candidate", example / "candidates" / "002-review-explanation.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("empty list", result.stdout)
        self.assertIn("no model was called", result.stdout)

    def test_judge_pass_and_forged_pass_verdicts(self):
        example = ROOT / "examples" / "judge"
        task = example / "tasks" / "002-review-explanation.md"
        good = self.invoke(RUNNER, "ingest-verdict", "--task", task,
                           "--verdict", example / "verdicts" / "good.json")
        bad = self.invoke(RUNNER, "ingest-verdict", "--task", task,
                          "--verdict", example / "verdicts" / "forged-pass.json")
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertTrue(json.loads(good.stdout)["passed"])
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        self.assertFalse(json.loads(bad.stdout)["passed"])


if __name__ == "__main__":
    unittest.main()
