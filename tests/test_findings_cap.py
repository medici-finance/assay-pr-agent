import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import cli, engine


def envelope(count):
    issues = [{"relevant_file": "app.py", "start_line": n, "end_line": n,
               "issue_header": f"Issue {n}", "issue_content": f"Problem on line {n}."} for n in range(1, count + 1)]
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": json.dumps({"review": {"key_issues_to_review": issues}})})


class FindingsCapTests(unittest.TestCase):
    anchors = {"app.py": set(range(1, 11))}

    def test_default_cap_is_six(self):
        self.assertEqual(engine.MAX_FINDINGS, 6)
        self.assertEqual(cli.DEFAULT_MAX_FINDINGS, 6)

    def test_answer_with_seven_findings_keeps_six_and_records_the_excess(self):
        for count in (6, 7):
            with self.subTest(count=count):
                answer = engine.parse_answer(envelope(count), self.anchors)
                self.assertEqual([f["line"] for f in answer["findings"]], [1, 2, 3, 4, 5, 6])
                self.assertEqual(answer["dropped"],
                                 [{"path": "app.py", "line": 7, "reason": "over the finding limit"}] if count == 7 else [])

    def test_prompt_asks_for_zero_to_six_confident_issues(self):
        prompt = engine.pack([], [])[0]
        self.assertIn("A concise list (0-6 issues)", prompt)
        self.assertNotIn("(0-3 issues)", prompt)
        self.assertIn("Only include issues you are confident about.", prompt)


class CacheKeyTests(unittest.TestCase):
    def test_an_answer_cached_under_another_cap_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False), \
                patch.object(engine, "provider", return_value="cursor"), \
                patch.object(engine, "invoke", return_value={"findings": [], "cursor_usage": None}) as call:
            os.environ.pop("REVIEW_CURSOR_TEAM_ID", None)
            cache = Path(tmp)
            old = engine.pack([], [], max_findings=3)[0]
            new = engine.pack([], [])[0]
            self.assertEqual(engine.review(old, None, {}, cache, "src", limit=3)[1], False)
            self.assertEqual(engine.review(old, None, {}, cache, "src", limit=3)[1], True)
            self.assertEqual(engine.review(new, None, {}, cache, "src")[1], False)
            self.assertEqual(engine.review(new, None, {}, cache, "src")[1], True)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(len(list(cache.glob("*.json"))), 2)


class RegularCommentSizeTests(unittest.TestCase):
    def report(self, findings, scanner_rows=40):
        rows = [{"path": "scripts/deploy.sh", "line": n, "rule": "SC2086",
                 "message": "Double quote to prevent globbing and word splitting."} for n in range(1, scanner_rows + 1)]
        return {"provider": "cursor", "state": "reviewed", "tier": "regular", "findings": findings,
                "checks": [{"tool": "shellcheck", "state": "complete", "findings": rows}], "omitted": ["vendor/blob.bin"],
                "degraded": []}

    def post(self, report):
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", "a" * 40, report)
        return post.call_args.args[3]

    @staticmethod
    def finding(n, message):
        return dict(path="src/app.py", line=n, end_line=n, title=f"Finding {n}", message=message)

    def test_prepared_review_from_before_the_setting_keeps_its_original_cap(self):
        report = self.report([self.finding(n, "A finding") for n in range(1, 11)], scanner_rows=0)
        report.update(tier="max", delivered="max", context=[], passes=[], omitted_findings=2,
                      verification={"state": "not-needed"}, max_input_tokens=60000, base_input_tokens=6000,
                      estimated_input_tokens=2000)
        self.assertIn("2 findings omitted by the 10-finding tier limit.", self.post(report))

    def test_realistic_six_findings_post_unchanged(self):
        findings = [self.finding(n, "x" * 600) for n in range(1, 7)]
        comment = self.post(self.report(findings))
        self.assertLess(len(comment.encode("utf-8")), cli.FORGE_COMMENT_BYTES)
        self.assertNotIn("shortened", comment)
        self.assertEqual(comment.count("x" * 600), 6)

    def test_six_long_findings_are_shortened_and_every_other_row_kept(self):
        # Three of these posted in full under the old cap; six would exceed the 16 KiB forge limit.
        long_message = "y" * 2600
        findings = [self.finding(1, "short and whole"), *(self.finding(n, long_message) for n in range(2, 7))]
        report = self.report(findings)
        three = self.report(findings[:3])
        self.assertNotIn("shortened", self.post(three))
        comment = self.post(report)
        self.assertLessEqual(len(comment.encode("utf-8")), cli.FORGE_COMMENT_BYTES)
        self.assertIn("Some findings were shortened to fit the forge comment.", comment)
        self.assertIn("` short and whole `\n", comment)
        lines = [line for line in comment.splitlines() if line.startswith("- ` src/app.py `")]
        self.assertEqual(len(lines), 6)
        for line in lines[1:]:
            self.assertTrue(line.endswith(" (shortened)"))
            self.assertGreaterEqual(line.count("y"), 140)
        # Scanner and coverage rows stay whole and nothing is counted as omitted.
        self.assertEqual(comment.count("SC2086"), 40)
        self.assertIn("- Outside review coverage: ` vendor/blob.bin `", comment)
        self.assertNotIn("omitted from this comment", comment)
        self.assertEqual(report["findings"][1]["message"], long_message)

    def test_shortened_findings_use_the_space_they_are_given(self):
        findings = [self.finding(n, "多" * 1700) for n in range(1, 7)]
        comment = self.post(self.report(findings, scanner_rows=0))
        size = len(comment.encode("utf-8"))
        self.assertLessEqual(size, cli.FORGE_COMMENT_BYTES)
        self.assertGreater(size, cli.FORGE_COMMENT_BYTES - 6 * 8)

    def test_shortening_reserves_space_for_a_finding_with_a_long_path(self):
        long_path = "p" * 1050 + ".py"
        findings = [dict(self.finding(1, "short and whole"), path=long_path),
                    self.finding(2, "y" * 3000)]
        comment = self.post(self.report(findings, scanner_rows=141))

        self.assertLessEqual(cli.size_of(comment), cli.FORGE_COMMENT_BYTES)
        self.assertIn("Some findings were shortened to fit the forge comment.", comment)
        self.assertIn(f"` {long_path} `:1", comment)
        self.assertIn("` short and whole `\n", comment)
        self.assertEqual(comment.count("SC2086"), 141)

    def test_falls_back_to_compact_rows_when_the_other_rows_leave_too_little_room(self):
        findings = [self.finding(n, "z" * 2000) for n in range(1, 7)]
        comment = self.post(self.report(findings, scanner_rows=300))
        self.assertLessEqual(len(comment.encode("utf-8")), cli.FORGE_COMMENT_BYTES)
        self.assertIn("Some review details were shortened to fit the forge comment.", comment)
        for n in range(1, 7):
            self.assertIn(f"Finding {n} ", comment)
        self.assertIn("check and history rows omitted from this comment", comment)


class UpperBoundCommentTests(unittest.TestCase):
    def test_comment_posts_at_the_largest_cap_with_worst_case_text_in_every_tier(self):
        wide = "\U0001F600"  # Four UTF-8 bytes per character.
        cap = cli.MAX_FINDINGS_LIMIT
        self.assertEqual(cap, 12)
        findings = [dict(path=wide * 300 + f"/{n}.py", line=99999, end_line=99999, title=wide * 2000,
                         message=wide * 2000) for n in range(cap)]
        for tier in cli.TIERS:
            with self.subTest(tier=tier):
                report = {"provider": "cursor", "state": "degraded", "tier": tier, "findings": findings,
                          "checks": [], "omitted": [wide * 300] * 5, "degraded": [wide * 300] * 60,
                          "full_input_tokens": 999999, "max_input_tokens": 60000, "prompt_limit": 105000,
                          "base_input_tokens": 6000, "estimated_input_tokens": 59999, "max_findings": cap,
                          "delivered": None if tier == "regular" else "regular",
                          "context": [{"name": name, "state": "dropped"} for name in
                                      ("change description", "base changes", "surrounding code", "call sites",
                                       "whole changed files", "call sites in context", "existing tests",
                                       "earlier comments")],
                          "passes": [{"name": "review", "state": "failed"}, {"name": "deep", "state": "failed"}],
                          "verification": {"state": "filtered", "before": 24, "after": cap}, "omitted_findings": 12}
                with patch.object(cli, "post_comment") as post:
                    cli.publish("org/repo#1", "a" * 40, report)
                comment = post.call_args.args[3]
                self.assertLessEqual(len(comment.encode("utf-8")), cli.FORGE_COMMENT_BYTES)
                self.assertEqual(comment.count(" — "), cap)


if __name__ == "__main__":
    unittest.main()
