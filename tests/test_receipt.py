import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cursor_pr import cli, engine, providers, receipt, source


APP = source.File("app.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")
OTHER = source.File("other.py", "@@ -0,0 +1 @@\n+y = 2\n", "added")
LOGO = source.File("logo.png", "", "added", True, known_no_text=True)


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
                                               "output_tokens": 1}), {"input": 7, "cached": 5, "cache_write": 1, "output": 1})
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
        estimate = receipt.build(report(state="estimate", model_invocations=0, coverage={"reviewed": 0, "text": 3}))
        self.assertEqual(receipt.render(estimate)[0],
                         "Receipt: estimate only, no model call. Prompt 2,314 of 6,000 estimated input tokens.")
        self.assertEqual(receipt.render(receipt.build(report(
                state="over-budget", model_invocations=0, estimated_input_tokens=9120, full_input_tokens=9120,
                coverage={"reviewed": 0, "text": 4})))[0],
                         "Receipt: over budget, no model call. Prompt 9,120 of 6,000 estimated input tokens.")
        tier_refusal = receipt.build(report(state="over-budget", estimated_input_tokens=11500,
                                            max_input_tokens=12000, prompt_limit=10500))
        self.assertIn("Prompt 11,500 of 10,500 estimated input tokens.", receipt.render(tier_refusal)[0])
        # A degraded review sent less than the whole change, so both sizes are worth stating.
        self.assertEqual(receipt.render(receipt.build(report(
                state="degraded", estimated_input_tokens=5000, full_input_tokens=9120)))[0],
                         "Receipt: 1 model call. Prompt 5,000 of 6,000 estimated input tokens; before budget trimming: 9,120.")
        self.assertEqual(receipt.render(receipt.build(report(coverage={"reviewed": 0, "text": 0, "no_text": 2})))[1],
                         "Coverage: no text files to review. Skipped for budget: 0. Not reviewable: 2. Outside the selection: 0. "
                         "Findings: 0 model and 0 static; 0 dropped; 0 without a quote to check. "
                         "Checks: 0 of 0 complete.")

    def test_estimate_and_model_failure_review_nothing(self):
        for flags, expected in ((["--estimate"], "estimate"), ([], "model-unavailable")):
            with self.subTest(state=expected), tempfile.TemporaryDirectory() as tmp:
                args = cli.parser().parse_args(["--change", "org/repo#1", "--repo", tmp, "--cache-dir", tmp,
                                                "--max-input-tokens", "6000", *flags])
                with patch.object(cli, "forge_change", return_value=source.Change("a" * 40, [APP], "org/repo#1")), \
                     patch.object(cli, "scan_files", return_value=[]), \
                     patch.object(engine, "invoke", side_effect=source.ReviewError("model failed")):
                    value, _ = cli.run_request(args)
                self.assertEqual(value["state"], expected)
                self.assertEqual((value["coverage"]["reviewed"], value["coverage"]["skipped"]), (0, 0))
                self.assertEqual(value["receipt"]["coverage"], {"reviewed": 0, "text": 1, "percent": 0})

    def test_receipt_reports_cache_reads_and_writes(self):
        line = receipt.render(receipt.build(report(model_usage={"cacheReadTokens": 66})))[0]
        self.assertIn("the CLI reported 66 cache read tokens", line)
        usage = {"input_tokens": 7, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1, "output_tokens": 2}
        line = receipt.render(receipt.build(report(model_usage=usage)))[0]
        self.assertIn("7 uncached input and 5 cache read and 1 cache write and 2 output tokens", line)

    def test_partial_coverage_does_not_round_to_full_or_zero(self):
        for reviewed, text, percent in ((199, 200, 99.5), (2999, 3000, 99.9), (1, 3000, 0.1)):
            with self.subTest(reviewed=reviewed, text=text):
                value = receipt.build(report(coverage={"reviewed": reviewed, "text": text}))
                self.assertEqual(value["coverage"]["percent"], percent)
                self.assertIn(f"({percent}%)", receipt.render(value)[1])

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
                                        "--max-input-tokens", "6000", "--path", "app.py"])

        def invoke(prompt, model, anchors, **kwargs):
            return {"findings": [], "dropped": [], "cursor_usage": {"input_tokens": 2900, "output_tokens": 120}}
        missing = source.File("big.py", "", "modified", absent=True)
        malformed = source.File("truncated.py", "@@ -1,2 +1,2 @@\n+x\n", "modified")
        change = source.Change("a" * 40, [APP, LOGO, OTHER, missing, malformed], "org/repo#1")
        with patch.object(cli, "forge_change", return_value=change), \
             patch.object(cli, "scan_files", return_value=[]), patch.object(engine, "invoke", side_effect=invoke), \
             patch.object(cli.drift, "forge", return_value={"state": "could-not-check", "behind": None, "both": [], "references": []}), \
             patch.object(cli, "post_comment", side_effect=lambda *args, **kwargs: posts.append(args[3])):
            report, code = cli.run_request(args)
            cli.publish("org/repo#1", report["head"], report)
        self.assertEqual((code, report["state"]), (2, "partial"))
        self.assertEqual(report["receipt"]["coverage"], {"reviewed": 1, "text": 2, "percent": 50})
        self.assertEqual(report["receipt"]["usage"], {"input": 2900, "output": 120})
        self.assertIn("Coverage: 1 of 2 text files reviewed (50%). Skipped for budget: 0. Not reviewable: 1. Outside the selection: 4. "
                      "Unavailable patches: 2.", posts[0])
        self.assertIn("; the CLI reported 2,900 input and 120 output tokens.", posts[0])
        self.assertTrue(posts[0].endswith(f"Checks: 0 of 0 complete.\n\n{providers.ATTRIBUTION}\n"))

    def test_visual_receipt_counts_hunks_shown_and_fits_in_the_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.parser().parse_args(["--change", "org/repo#1", "--repo", tmp, "--cache-dir", tmp,
                                            "--max-input-tokens", "6000", "--visual", "--post"])
            license = source.File("LICENSE", "@@ -0,0 +1 @@\n+License text\n", "added")
            change = source.Change("a" * 40, [APP, license], "org/repo#1")
            result = {"visual": {"summary": "Adds configuration.", "changes": [], "hotspots": []}, "cursor_usage": None}
            with patch.object(cli, "forge_change", return_value=change), \
                 patch.object(cli, "scan_files", return_value=[]), patch.object(engine, "invoke", return_value=result), \
                 patch.object(cli, "evidence", return_value=["line " + "x" * 300] * 100), \
                 patch.object(cli, "post_comment") as post:
                value, _ = cli.run_request(args)
        self.assertEqual(value["receipt"]["coverage"], {"reviewed": 1, "text": 2, "percent": 50})
        body = post.call_args.args[3]
        self.assertLessEqual(len(body.encode()), cli.visual.MAX_COMMENT_BYTES)
        self.assertIn("Coverage: 1 of 2 text files reviewed (50%).", body)
        self.assertIn("Text counted only: 1.", body)
        self.assertTrue(body.endswith(providers.ATTRIBUTION + "\n"))

    def test_cache_hit_does_not_republish_the_same_review(self):
        project = SimpleNamespace(key="github/org/repo", visibility="private")
        value = report(omitted=["big.py"], coverage={"reviewed": 1, "text": 1, "excluded": 1, "unavailable": 1})
        head = "a" * 40
        with tempfile.TemporaryDirectory() as tmp, patch.object(source, "run_forge") as forge:
            for invocations in (1, 0):
                value.update(model_invocations=invocations, cache_hit=not invocations)
                value["receipt"] = receipt.build(value)
                cli.publish("org/repo#1", head, value, project=project, state=Path(tmp))
            value["coverage"].update(text=2, unavailable=0)
            value["receipt"] = receipt.build(value)
            cli.publish("org/repo#1", head, value, project=project, state=Path(tmp))
            first, second, corrected = (call.kwargs["publication"] for call in forge.call_args_list)
            self.assertFalse(first["AlreadyPosted"])
            self.assertTrue(second["AlreadyPosted"])
            self.assertNotEqual(first["Body"], second["Body"])
            self.assertFalse(corrected["AlreadyPosted"])
            self.assertIn("Coverage: 1 of 2 text files reviewed (50%).", corrected["Body"])

    def test_check_count_can_change_without_a_change_in_visible_evidence(self):
        project = SimpleNamespace(key="github/org/repo", visibility="private")
        head = "a" * 40
        finding = {"path": "app.py", "line": 1, "rule": "example", "message": "x" * 200}
        check = {"tool": "example", "state": "complete", "findings": [finding] * 100}
        extra = {"tool": "another", "state": "could-not-check", "reason": "x" * 350, "findings": []}
        reading = {"summary": "Adds configuration.", "changes": [], "hotspots": []}
        diff_map = dict(cli.visual.survey([APP]), shown=[APP.path])
        value = report(head=head, omitted=[], map=diff_map, visual=reading, checks=[check, extra])
        with tempfile.TemporaryDirectory() as tmp, patch.object(source, "run_forge") as forge:
            baseline = cli.visual.comment(value, cli.evidence(value))
            value["receipt"] = receipt.build(value)
            cli.publish("org/repo#1", head, value, project=project, state=Path(tmp))
            extra["state"] = "complete"
            extra["findings"] = [finding]
            self.assertEqual(cli.visual.comment(value, cli.evidence(value)), baseline)
            value["receipt"] = receipt.build(value)
            cli.publish("org/repo#1", head, value, project=project, state=Path(tmp))
        updated = forge.call_args.kwargs["publication"]
        self.assertFalse(updated["AlreadyPosted"])
        self.assertIn("Checks: 2 of 2 complete.", updated["Body"])

    def test_pending_publication_from_an_older_worker_still_deduplicates(self):
        project = SimpleNamespace(key="github/org/repo", visibility="private")
        head = "a" * 40
        old = report(omitted=[])
        del old["coverage"]
        with tempfile.TemporaryDirectory() as tmp, patch.object(source, "run_forge") as forge:
            cli.publish("org/repo#1", head, old, project=project, state=Path(tmp))
            old["receipt"] = receipt.build(old)
            cli.publish("org/repo#1", head, old, project=project, state=Path(tmp))
        retry = forge.call_args.kwargs["publication"]
        self.assertTrue(retry["AlreadyPosted"])
        self.assertIn("Coverage: counts unavailable for this saved review.", retry["Body"])


if __name__ == "__main__":
    unittest.main()
