import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_pr import cli, engine, receipt, source


APP = source.File("app.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")
OTHER = source.File("other.py", "@@ -0,0 +1 @@\n+y = 2\n", "added")
LOGO = source.File("logo.png", "", "added", True)


def report(**extra):
    base = {"provider": "claude", "state": "reviewed", "model_invocations": 1, "cache_hit": False,
            "estimated_input_tokens": 2314, "max_input_tokens": 6000, "full_input_tokens": 2314, "model_usage": None,
            "findings": [], "checks": [],
            "coverage": {"changed": 3, "text": 3, "reviewed": 3, "skipped": 0, "excluded": 0, "no_text": 0}}
    return dict(base, **extra)


class ReceiptTests(unittest.TestCase):
    def test_usage_is_read_from_each_cli(self):
        self.assertEqual(receipt.usage_totals({"inputTokens": 4, "cacheReadTokens": 66, "outputTokens": 9}),
                         {"input": 4, "cached": 66, "output": 9})
        self.assertEqual(receipt.usage_totals({"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 2}),
                         {"input": 10, "cached": 3, "output": 2})
        self.assertEqual(receipt.usage_totals({"input_tokens": 7, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1,
                                               "output_tokens": 1}), {"input": 7, "cached": 5, "output": 1})
        self.assertEqual(receipt.usage_totals({"input_tokens": True, "output_tokens": "9"}), {})
        self.assertEqual(receipt.usage_totals(None), {})

    def test_lines_name_the_cost_and_the_coverage(self):
        lines = receipt.render(receipt.build(report(
            model_usage={"input_tokens": 2900, "output_tokens": 120},
            findings=[{"path": "a", "evidence": "verified"}, {"path": "b", "evidence": "unverified"}],
            checks=[{"state": "complete", "findings": []}, {"state": "could-not-check", "findings": []}],
            static_checks=[{"state": "complete", "findings": [{}, {}]}], dropped=[{}, {}])))
        self.assertEqual(lines, [
            "Receipt: 1 model call. Prompt 2,314 of 6,000 estimated input tokens; the CLI reported 2,900 input and 120 output tokens.",
            "Coverage: 3 of 3 text files reviewed (100%). Skipped for budget: 0. Not reviewable: 0. Outside the selection: 0. "
            "Findings: 2 model and 2 static; 2 dropped; 1 without a quote to check. Checks: 2 of 3 complete."])

    def test_states_without_a_model_call_and_without_text(self):
        self.assertEqual(receipt.render(receipt.build(report(state="estimate", model_invocations=0)))[0],
                         "Receipt: estimate only, no model call. Prompt 2,314 of 6,000 estimated input tokens.")
        self.assertEqual(receipt.render(receipt.build(report(
                state="over-budget", model_invocations=0, estimated_input_tokens=9120, full_input_tokens=9120,
                coverage={"reviewed": 0, "text": 4})))[0],
                         "Receipt: over budget, no model call. Prompt 9,120 of 6,000 estimated input tokens.")
        # A degraded review sent less than the whole change, so both sizes are worth stating.
        self.assertEqual(receipt.render(receipt.build(report(
                state="degraded", estimated_input_tokens=5000, full_input_tokens=9120)))[0],
                         "Receipt: 1 model call. Prompt 5,000 of 6,000 estimated input tokens; the complete change needs 9,120.")
        self.assertEqual(receipt.render(receipt.build(report(cache_hit=True, coverage={"reviewed": 0, "text": 0})))[1],
                         "Coverage: no text files to review. Findings: 0 model and 0 static; 0 dropped; 0 without a quote to check. "
                         "Checks: 0 of 0 complete.")

    def test_a_model_failure_reviews_nothing(self):
        built = receipt.build(report(state="model-unavailable"))
        self.assertEqual(built["coverage"], {"reviewed": 0, "text": 3, "percent": 0})

    def test_usage_with_only_cached_tokens_is_not_stated(self):
        line = receipt.render(receipt.build(report(model_usage={"cacheReadTokens": 66})))[0]
        self.assertEqual(line, "Receipt: 1 model call. Prompt 2,314 of 6,000 estimated input tokens.")

    def test_a_degraded_review_counts_the_files_it_kept(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        big = source.File("big.py", "@@ -0,0 +1,400 @@\n" + "".join(f"+value_{n} = compute({n})\n" for n in range(400)), "added")
        args = cli.parser().parse_args(["--change", "org/repo#1", "--repo", tmp.name, "--cache-dir", str(Path(tmp.name) / "cache"),
                                        "--max-input-tokens", "2500"])
        with patch.object(cli, "forge_change", return_value=source.Change("a" * 40, [APP, big], "org/repo#1")), \
             patch.object(cli, "scan_files", return_value=[]), \
             patch.object(engine, "invoke", return_value={"findings": [], "dropped": [], "cursor_usage": None}), \
             patch.object(cli.drift, "forge", return_value={"state": "could-not-check", "behind": None, "both": [], "references": []}):
            report, _ = cli.run_request(args)
        self.assertEqual((report["state"], report["degraded"]), ("degraded", ["big.py"]))
        self.assertEqual((report["coverage"]["reviewed"], report["coverage"]["skipped"]), (1, 1))
        self.assertEqual(report["receipt"]["coverage"], {"reviewed": 1, "text": 2, "percent": 50})

    def test_the_lines_carry_nothing_from_the_change(self):
        hostile = report(findings=[{"path": "@all", "title": "[x](https://evil.example)", "message": "y"}],
                         model_usage={"input_tokens": 5})
        text = "".join(receipt.render(receipt.build(hostile)))
        self.assertNotIn("@all", text)
        self.assertNotIn("evil.example", text)

    def test_a_review_ends_its_summary_with_the_receipt(self):
        posts = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        args = cli.parser().parse_args(["--change", "org/repo#1", "--repo", tmp.name, "--cache-dir", str(Path(tmp.name) / "cache"),
                                        "--max-input-tokens", "6000", "--path", "app.py", "--path", "logo.png"])

        def invoke(prompt, model, anchors):
            return {"findings": [], "dropped": [], "cursor_usage": {"input_tokens": 2900, "output_tokens": 120}}
        with patch.object(cli, "forge_change", return_value=source.Change("a" * 40, [APP, LOGO, OTHER], "org/repo#1")), \
             patch.object(cli, "scan_files", return_value=[]), patch.object(engine, "invoke", side_effect=invoke), \
             patch.object(cli.drift, "forge", return_value={"state": "could-not-check", "behind": None, "both": [], "references": []}), \
             patch.object(cli, "post_comment", side_effect=lambda *args: posts.append(args[3])):
            report, code = cli.run_request(args)
            cli.publish("org/repo#1", report["head"], report)
        self.assertEqual((code, report["state"]), (2, "partial"))
        self.assertEqual(report["receipt"]["coverage"], {"reviewed": 1, "text": 2, "percent": 50})
        self.assertEqual(report["receipt"]["usage"], {"input": 2900, "output": 120})
        self.assertIn("Coverage: 1 of 2 text files reviewed (50%). Skipped for budget: 0. Not reviewable: 1. Outside the selection: 1.", posts[0])
        self.assertIn("; the CLI reported 2,900 input and 120 output tokens.", posts[0])
        self.assertTrue(posts[0].endswith("Checks: 0 of 0 complete.\n\nReview tooling built on [PR-Agent](https://github.com/the-pr-agent/pr-agent).\n"))


if __name__ == "__main__":
    unittest.main()
