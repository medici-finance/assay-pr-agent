import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_pr import cli, engine, outcomes, source


CHANGE_URL = "https://github.com/org/repo/pull/7"


def finding(path, line, title="Possible Bug", end=None):
    return {"path": path, "line": line, "end_line": end or line, "title": title, "message": "Why it matters.",
            "evidence": "unverified"}


def anchors(files):
    return {path: {n + 1: text for n, text in enumerate(lines)} for path, lines in files.items()}


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        environment = patch.dict(os.environ, {"REVIEW_STATE_DIR": str(self.tmp)})
        environment.start()
        self.addCleanup(environment.stop)

    def record(self, head, findings, shown, address=CHANGE_URL, changed_paths=None):
        return outcomes.record(self.tmp, address, head, findings, shown, changed_paths)

    def review(self, head, lines, answer, *flags, fail_post=False, repo=None, patch_text=None):
        posts = []
        if patch_text is None and lines:
            patch_text = "@@ -0,0 +1,%d @@\n" % len(lines) + "".join("+" + line + "\n" for line in lines)
        args = cli.parser().parse_args(["--change", "org/repo#7", "--repo", str(repo or self.tmp), "--cache-dir", str(self.tmp / "cache"),
                                        "--max-input-tokens", "6000", *flags])
        files = [source.File("app.py", patch_text, "added")] if patch_text else []
        def post(*args, **kwargs):
            if fail_post:
                raise source.ReviewError("Head moved")
            posts.append(args[3])
        with patch.object(cli, "forge_change", return_value=source.Change(head, files, CHANGE_URL)), \
             patch.object(cli, "scan_files", return_value=[]), \
             patch.object(engine, "invoke", return_value={"findings": answer, "dropped": [], "cursor_usage": None}), \
             patch.object(cli.drift, "forge", return_value={"state": "could-not-check", "behind": None, "both": [], "references": []}), \
             patch.object(cli, "post_comment", side_effect=post):
            report, code = cli.run_request(args)
            if not args.post:
                cli.publish("org/repo#7", head, report)
        return report, posts[0] if posts else None

    def test_a_first_review_only_opens_findings(self):
        first = self.record("a" * 40, [finding("a.py", 2)], anchors({"a.py": ["x = 1", "y = 2"]}))
        self.assertEqual(first, {"fixed": 0, "unaddressed": 0, "gone": 0, "repeated": 0, "open": 1, "accept_rate": None})

    def test_a_later_review_resolves_each_earlier_finding(self):
        self.record("a" * 40, [finding("a.py", 2), finding("a.py", 1, "Other"), finding("b.py", 1, "Elsewhere"),
                               finding("c.py", 1, "Skipped")],
                    anchors({"a.py": ["x = 1", "y = 2"], "b.py": ["z = 3"], "c.py": ["q = 4"]}))
        # "Other" is raised again; "Possible Bug" is not and its line changed; b.py is unchanged; c.py is out of scope.
        later = self.record("b" * 40, [finding("a.py", 2, "Other")], anchors({"a.py": ["x = 1", "y = (2)"], "b.py": ["z = 3"]}))
        self.assertEqual(later, {"fixed": 1, "unaddressed": 1, "gone": 0, "repeated": 1, "open": 2, "accept_rate": 50})

    def test_the_same_head_and_another_change_resolve_nothing(self):
        shown = anchors({"a.py": ["x = 1", "y = 2"]})
        self.record("a" * 40, [finding("a.py", 2)], shown)
        self.assertEqual(self.record("a" * 40, [], shown)["open"], 1)
        self.assertEqual(self.record("b" * 40, [], shown, address="https://gitlab.com/org/repo/-/merge_requests/7"),
                         {"fixed": 0, "unaddressed": 0, "gone": 0, "repeated": 0, "open": 0, "accept_rate": None})

    def test_a_finding_raised_again_is_counted_once_when_fixed(self):
        shown = anchors({"a.py": ["x = 1", "y = 2"]})
        self.record("a" * 40, [finding("a.py", 2)], shown)
        self.record("b" * 40, [finding("a.py", 2)], shown)
        later = self.record("c" * 40, [], anchors({"a.py": ["x = 1", "y = (2)"]}))
        self.assertEqual(later, {"fixed": 1, "unaddressed": 0, "gone": 0, "repeated": 1, "open": 0, "accept_rate": 100})

    def test_an_unaddressed_finding_fixed_at_a_later_head_is_fixed(self):
        shown = anchors({"a.py": ["x = 1", "y = 2"]})
        self.record("a" * 40, [finding("a.py", 2)], shown)
        self.assertEqual(self.record("b" * 40, [], shown)["unaddressed"], 1)
        later = self.record("c" * 40, [], anchors({"a.py": ["x = 1", "y = (2)"]}))
        self.assertEqual((later["fixed"], later["unaddressed"]), (1, 0))

    def test_a_file_left_out_of_the_prompt_resolves_nothing(self):
        self.record("a" * 40, [finding("a.py", 2)], anchors({"a.py": ["x = 1", "y = 2"]}))
        # Left out for the budget, or served with no text: the file has no anchors at this head.
        later = self.record("b" * 40, [], {"a.py": {}})
        self.assertEqual((later["fixed"], later["open"]), (0, 1))

    def test_reverting_the_whole_change_to_a_file_is_gone_not_fixed(self):
        self.review("a" * 40, ["x = 1"], [finding("app.py", 1)], "--post")
        later, body = self.review("b" * 40, [], [], "--post")
        self.assertEqual((later["state"], later["outcomes"]["fixed"], later["outcomes"]["gone"]), ("empty", 0, 1))
        self.assertIsNone(later["outcomes"]["accept_rate"])
        self.assertIn("0 fixed and 0 left unchanged at a later review (accept rate not available); "
                      "1 gone from the change with no sign of a fix", body)

    def test_a_hunk_that_left_the_diff_is_gone_not_fixed(self):
        # Reverted to the base, or rebased into it: the diff no longer shows the place at all.
        self.record("a" * 40, [finding("a.py", 2)], anchors({"a.py": ["before()", "suspect()", "after()"]}))
        later = self.record("b" * 40, [], {"a.py": {20: "other_change()"}}, changed_paths={"a.py"})
        self.assertEqual((later["fixed"], later["gone"], later["open"], later["accept_rate"]), (0, 1, 0, None))

    def test_a_changed_line_next_to_either_recorded_neighbor_is_fixed(self):
        for shown in ({1: "before()", 2: "safe()", 30: "elsewhere()"}, {7: "other()", 8: "after()"},
                      {1: "before()", 2: "", 3: "after()"}):
            with self.subTest(shown=shown):
                (self.tmp / outcomes.NAME).unlink(missing_ok=True)
                self.record("a" * 40, [finding("a.py", 2)], anchors({"a.py": ["before()", "suspect()", "after()"]}))
                later = self.record("b" * 40, [], {"a.py": shown}, changed_paths={"a.py"})
                self.assertEqual((later["fixed"], later["gone"], later["accept_rate"]), (1, 0, 100))

    def test_a_line_number_or_a_neighbor_elsewhere_is_no_evidence_of_a_fix(self):
        # The cited lines were all that was shown; a rebase puts other code at the same line number.
        self.record("a" * 40, [finding("a.py", 2, end=3)], {"a.py": {2: "suspect()", 3: "more()"}})
        later = self.record("b" * 40, [], {"a.py": {2: "unrelated()"}}, changed_paths={"a.py"})
        self.assertEqual((later["fixed"], later["gone"]), (0, 1))
        # A neighbor from another hunk says nothing about the cited place.
        (self.tmp / outcomes.NAME).unlink()
        self.record("a" * 40, [finding("a.py", 9)], {"a.py": {1: "before()", 9: "suspect()"}})
        later = self.record("b" * 40, [], {"a.py": {1: "before()", 2: "other()"}}, changed_paths={"a.py"})
        self.assertEqual((later["fixed"], later["gone"]), (0, 1))
        # Nor does a neighbor that now occurs twice.
        (self.tmp / outcomes.NAME).unlink()
        self.record("a" * 40, [finding("a.py", 2)], anchors({"a.py": ["}", "suspect()"]}))
        later = self.record("b" * 40, [], anchors({"a.py": ["}", "other()", "}", "more()"]}), changed_paths={"a.py"})
        self.assertEqual((later["fixed"], later["gone"]), (0, 1))

    def test_rows_fixed_under_the_old_rule_become_gone(self):
        self.record("a" * 40, [finding("a.py", 2)], anchors({"a.py": ["x = 1", "y = 2"]}))
        with sqlite3.connect(self.tmp / outcomes.NAME) as db:
            db.execute("UPDATE findings SET status='fixed', later_head=?", ("b" * 40,))
            db.execute("PRAGMA user_version=0")
        later = self.record("c" * 40, [], anchors({"a.py": ["x = 1"]}))
        self.assertEqual((later["fixed"], later["gone"], later["accept_rate"]), (0, 1, None))
        with sqlite3.connect(self.tmp / outcomes.NAME) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        # A row fixed under the new rule stays fixed.
        self.record("d" * 40, [finding("a.py", 1, "New")], anchors({"a.py": ["x = 1", "y = 3"]}))
        self.assertEqual(self.record("e" * 40, [], anchors({"a.py": ["x = 2", "y = 3"]}))["fixed"], 1)

    def test_another_identical_line_does_not_stand_in_for_the_fixed_occurrence(self):
        self.record("a" * 40, [finding("a.py", 2)], anchors({
            "a.py": ["before_first()", "suspect()", "after_first()", "before_second()", "suspect()", "after_second()"]}))
        later = self.record("b" * 40, [], anchors({
            "a.py": ["before_first()", "corrected()", "after_first()", "before_second()", "suspect()", "after_second()"]}))
        self.assertEqual((later["fixed"], later["unaddressed"]), (1, 0))

    def test_reworded_repeat_is_counted_once_and_shown_in_comment(self):
        self.review("a" * 40, ["starter()", "suspect()"], [finding("app.py", 2)], "--post")
        later, body = self.review("b" * 40, ["starter()", "suspect()", "extra()"],
                                  [finding("app.py", 2, "Reworded finding")], "--post")
        self.assertEqual((later["outcomes"]["repeated"], later["outcomes"]["unaddressed"]), (1, 0))
        self.assertIn("accept rate not available", body)

    def test_interior_whitespace_is_part_of_the_cited_code(self):
        self.record("a" * 40, [finding("a.py", 2)], anchors({"a.py": ["before()", 'x = "a  b"']}))
        later = self.record("b" * 40, [], anchors({"a.py": ["before()", 'x = "a b"']}))
        self.assertEqual((later["fixed"], later["unaddressed"]), (1, 0))

    def test_blank_cited_lines_do_not_count_as_a_change(self):
        self.record("a" * 40, [finding("a.py", 1, end=2)], anchors({"a.py": ["", "y = 2"]}))
        later = self.record("b" * 40, [], anchors({"a.py": ["y = 2"]}))
        self.assertEqual((later["unaddressed"], later["fixed"]), (1, 0))

    def test_reviews_of_one_change_track_what_happens_to_findings(self):
        first, body = self.review("a" * 40, ["x = 1", "y = 2"], [finding("app.py", 2)], "--post")
        self.assertEqual((first["outcomes"]["open"], "Tracked findings" in body), (1, False))
        second, body = self.review("b" * 40, ["x = 1", "y = (2)"], [], "--post")
        self.assertEqual((second["state"], second["outcomes"]["accept_rate"]), ("reviewed", 100))
        self.assertIn("Tracked findings for this change: 1 fixed and 0 left unchanged at a later review "
                      "(accept rate 100%); 0 gone from the change with no sign of a fix; 0 raised again; "
                      "0 awaiting a later review.", body)
        # A different patch, so the model is asked again rather than the cached answer reused.
        third, body = self.review("c" * 40, ["x = 1", "y = (2)", "z = 3"], [finding("app.py", 3, "New")], "--post")
        self.assertEqual(third["outcomes"], {"fixed": 1, "unaddressed": 0, "gone": 0, "repeated": 0, "open": 1, "accept_rate": 100})
        self.assertIn("1 fixed and 0 left unchanged", body)

    def test_nothing_is_tracked_without_a_state_directory_or_when_it_cannot_be_written(self):
        with patch.dict(os.environ, {"REVIEW_STATE_DIR": str(self.tmp / "missing")}):
            report, _ = self.review("a" * 40, ["x = 1"], [finding("app.py", 1)], "--post")
        self.assertNotIn("outcomes", report)
        # A review that posts nothing records nothing: the author never saw its findings.
        report, _ = self.review("c" * 40, ["x = 2"], [finding("app.py", 1)])
        self.assertNotIn("outcomes", report)
        self.assertFalse((self.tmp / outcomes.NAME).exists())
        with patch.object(outcomes, "record", side_effect=OSError("read-only")):
            report, _ = self.review("b" * 40, ["x = 1"], [finding("app.py", 1)], "--post")
        self.assertEqual((report["state"], "outcomes" in report), ("reviewed", False))

    def test_a_failed_publication_does_not_record_or_resolve_a_finding(self):
        failed, _ = self.review("a" * 40, ["x = 1"], [finding("app.py", 1)], "--post", fail_post=True)
        self.assertEqual(failed["publication"], "failed")
        self.assertFalse((self.tmp / outcomes.NAME).exists())
        self.review("b" * 40, ["x = 1"], [finding("app.py", 1)], "--post")
        failed, _ = self.review("c" * 40, ["x = 2"], [], "--post", fail_post=True)
        self.assertNotIn("outcomes", failed)
        observed = outcomes.record(self.tmp, CHANGE_URL, "b" * 40, [], {}, persist=False)
        self.assertEqual((observed["open"], observed["fixed"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
