import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_pr import cli, engine, source


ADDED = source.File("app.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")
DELETED = source.File("old.py", "@@ -1 +0,0 @@\n-x = 1\n", "removed")
BINARY = source.File("logo.png", "", "added", True)
NO_DRIFT = {"state": "could-not-check", "behind": None, "both": [], "references": []}


class CoverageStateTests(unittest.TestCase):
    def run_review(self, files, *flags):
        shown, posts = [], []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        args = cli.parser().parse_args(["--change", "org/repo#1", "--repo", tmp.name, "--cache-dir", str(Path(tmp.name) / "cache"),
                                        "--max-input-tokens", "6000", *flags])

        def invoke(prompt, model, anchors):
            shown.append(prompt)
            return {"findings": [], "dropped": [], "model_usage": None}
        with patch.object(cli, "forge_change", return_value=source.Change("a" * 40, files, "org/repo#1")), \
             patch.object(cli.drift, "forge", return_value=NO_DRIFT), patch.object(cli, "scan_files", return_value=[]), \
             patch.object(engine, "invoke", side_effect=invoke), \
             patch.object(cli, "post_comment", side_effect=lambda *args: posts.append(args[3])):
            report, code = cli.run_request(args)
            cli.publish("org/repo#1", report["head"], report)
        return report, code, shown, posts

    def test_files_with_no_text_do_not_make_a_review_partial(self):
        report, code, shown, posts = self.run_review([BINARY, ADDED, DELETED])
        self.assertEqual((code, report["state"], report["omitted"]), (0, "reviewed", []))
        self.assertEqual(report["not_reviewable"], ["logo.png", "old.py"])
        self.assertEqual(len(shown), 1)
        self.assertIn("- Not reviewed, no text to review: ` logo.png `, ` old.py `", posts[0])
        self.assertNotIn("Outside review coverage", posts[0])
        self.assertNotIn("Degraded review", posts[0])

    def test_a_change_with_nothing_to_review_is_empty(self):
        report, code, shown, _ = self.run_review([DELETED, BINARY])
        self.assertEqual((code, report["state"], shown), (0, "empty", []))
        self.assertEqual(report["not_reviewable"], ["logo.png", "old.py"])
        self.assertEqual(engine.pack([DELETED], [])[2:], (["old.py"], False))

    def test_what_is_left_out_of_a_review_still_makes_it_partial(self):
        other = source.File("other.py", "@@ -0,0 +1 @@\n+y = 2\n", "added")
        report, code, _, posts = self.run_review([ADDED, other], "--path", "app.py")
        self.assertEqual((code, report["state"]), (2, "partial"))
        self.assertEqual((report["omitted"], report["not_reviewable"]), (["other.py"], []))
        self.assertIn("- Outside review coverage: ` other.py `", posts[0])
        self.assertNotIn("Not reviewed, no text", posts[0])


if __name__ == "__main__":
    unittest.main()
