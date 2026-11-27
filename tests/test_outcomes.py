import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_pr import cli, engine, outcomes, source


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

    def record(self, head, findings, shown, address="org/repo#7"):
        return outcomes.record(self.tmp, address, head, findings, shown)

    def review(self, head, lines, answer, *flags):
        posts = []
        patch_text = "@@ -0,0 +1,%d @@\n" % len(lines) + "".join("+" + line + "\n" for line in lines)
        args = cli.parser().parse_args(["--change", "org/repo#7", "--repo", str(self.tmp), "--cache-dir", str(self.tmp / "cache"),
                                        "--max-input-tokens", "6000", *flags])
        with patch.object(cli, "forge_change", return_value=source.Change(head, [source.File("app.py", patch_text, "added")], "org/repo#7")), \
             patch.object(cli, "scan_files", return_value=[]), \
             patch.object(engine, "invoke", return_value={"findings": answer, "dropped": [], "model_usage": None}), \
             patch.object(cli.drift, "forge", return_value={"state": "could-not-check", "behind": None, "both": [], "references": []}), \
             patch.object(cli, "post_comment", side_effect=lambda *args: posts.append(args[3])):
            report, code = cli.run_request(args)
            if not args.post:
                cli.publish("org/repo#7", head, report)
        return report, posts[0] if posts else None

    def test_a_first_review_only_opens_findings(self):
        first = self.record("a" * 40, [finding("a.py", 2)], anchors({"a.py": ["x = 1", "y = 2"]}))
        self.assertEqual(first, {"fixed": 0, "unaddressed": 0, "repeated": 0, "open": 1, "accept_rate": None})

    def test_a_later_review_resolves_each_earlier_finding(self):
        self.record("a" * 40, [finding("a.py", 2), finding("a.py", 1, "Other"), finding("b.py", 1, "Elsewhere"),
                               finding("c.py", 1, "Skipped")],
                    anchors({"a.py": ["x = 1", "y = 2"], "b.py": ["z = 3"], "c.py": ["q = 4"]}))
        # "Other" is raised again; "Possible Bug" is not and its line changed; b.py is unchanged; c.py is out of scope.
        later = self.record("b" * 40, [finding("a.py", 2, "Other")], anchors({"a.py": ["x = 1", "y = (2)"], "b.py": ["z = 3"]}))
        self.assertEqual(later, {"fixed": 1, "unaddressed": 1, "repeated": 1, "open": 2, "accept_rate": 50})

    def test_the_same_head_and_another_change_resolve_nothing(self):
        shown = anchors({"a.py": ["x = 1", "y = 2"]})
        self.record("a" * 40, [finding("a.py", 2)], shown)
        self.assertEqual(self.record("a" * 40, [], shown)["open"], 1)
        self.assertEqual(self.record("b" * 40, [], shown, address="org/repo#8"),
                         {"fixed": 0, "unaddressed": 0, "repeated": 0, "open": 0, "accept_rate": None})

    def test_a_finding_raised_again_is_counted_once_when_fixed(self):
        shown = anchors({"a.py": ["x = 1", "y = 2"]})
        self.record("a" * 40, [finding("a.py", 2)], shown)
        self.record("b" * 40, [finding("a.py", 2)], shown)
        later = self.record("c" * 40, [], anchors({"a.py": ["x = 1", "y = (2)"]}))
        self.assertEqual(later, {"fixed": 1, "unaddressed": 0, "repeated": 1, "open": 0, "accept_rate": 100})

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

    def test_blank_cited_lines_do_not_count_as_a_change(self):
        self.record("a" * 40, [finding("a.py", 1, end=2)], anchors({"a.py": ["", "y = 2"]}))
        later = self.record("b" * 40, [], anchors({"a.py": ["y = 2"]}))
        self.assertEqual((later["unaddressed"], later["fixed"]), (1, 0))

    def test_reviews_of_one_change_track_what_happens_to_findings(self):
        first, body = self.review("a" * 40, ["x = 1", "y = 2"], [finding("app.py", 2)], "--post")
        self.assertEqual((first["outcomes"]["open"], "Tracked findings" in body), (1, False))
        second, body = self.review("b" * 40, ["x = 1", "y = (2)"], [], "--post")
        self.assertEqual((second["state"], second["outcomes"]["accept_rate"]), ("reviewed", 100))
        self.assertIn("Tracked findings for this change: 1 addressed and 0 left unchanged at a later review "
                      "(accept rate 100%); 0 raised again; 0 awaiting a later review.", body)
        # A different patch, so the model is asked again rather than the cached answer reused.
        third, body = self.review("c" * 40, ["x = 1", "y = (2)", "z = 3"], [finding("app.py", 3, "New")], "--post")
        self.assertEqual(third["outcomes"], {"fixed": 1, "unaddressed": 0, "repeated": 0, "open": 1, "accept_rate": 100})
        self.assertIn("1 addressed and 0 left unchanged", body)

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


if __name__ == "__main__":
    unittest.main()
