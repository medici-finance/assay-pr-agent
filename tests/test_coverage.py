import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_pr import cli, engine, source


ADDED = source.File("app.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")
DELETED = source.File("old.py", "@@ -1 +0,0 @@\n-x = 1\n", "removed")
BINARY = source.File("logo.png", "", "added", True, known_no_text=True)
NO_DRIFT = {"state": "could-not-check", "behind": None, "both": [], "references": []}


class CoverageStateTests(unittest.TestCase):
    def run_review(self, files, *flags, repo=None, head="a" * 40):
        shown, posts = [], []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        args = cli.parser().parse_args(["--change", "org/repo#1", "--repo", repo or tmp.name, "--cache-dir", str(Path(tmp.name) / "cache"),
                                        "--max-input-tokens", "6000", *flags])

        def invoke(prompt, model, anchors):
            shown.append(prompt)
            return {"findings": [], "dropped": [], "model_usage": None}
        with patch.object(cli, "forge_change", return_value=source.Change(head, files, "org/repo#1")), \
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

    def test_an_unavailable_patch_makes_a_review_partial(self):
        missing = source.File("app.py", "", "modified", True)
        removed = source.File("removed.py", "", "removed", True)
        report, code, shown, posts = self.run_review([missing, removed])
        self.assertEqual((code, report["state"], shown), (2, "partial", []))
        self.assertEqual((report["omitted"], report["not_reviewable"]), (["app.py"], ["removed.py"]))
        self.assertIn("- Outside review coverage: ` app.py `", posts[0])

    def head_repo(self, blobs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, data in blobs.items():
            (root / name).write_bytes(data)
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
        for command in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
            subprocess.run(["git", "-C", tmp.name, *command], check=True, env=env)
        head = subprocess.run(["git", "-C", tmp.name, "rev-parse", "HEAD"], check=True, env=env,
                              capture_output=True, text=True).stdout.strip()
        return tmp.name, head

    def test_a_missing_patch_on_a_text_blob_is_lost_coverage_and_on_a_binary_blob_is_not(self):
        repo, head = self.head_repo({"big.py": b"x = 1\n" * 50_000, "blob.dat.txt": b"PK\x03\x04\0\0rest",
                                     "empty.py": b"", "moved.py": b"y = 2\n"})
        files = [source.File("big.py", "", "modified", True), source.File("blob.dat.txt", "", "added", True),
                 source.File("empty.py", "", "added", True), source.File("moved.py", "", "renamed", True, previous="old.py")]
        report, code, shown, posts = self.run_review(files, repo=repo, head=head)
        self.assertEqual((code, report["state"], shown), (2, "partial", []))
        self.assertEqual(report["omitted"], ["big.py"])
        self.assertEqual(report["not_reviewable"], ["blob.dat.txt", "empty.py", "moved.py"])
        self.assertIn("- Outside review coverage: ` big.py `", posts[0])

    def test_binary_files_alone_are_not_partial(self):
        repo, head = self.head_repo({"logo.png": b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR", "app.py": b"x = 1\n"})
        report, code, _, _ = self.run_review([source.File("logo.png", "", "modified", True), ADDED], repo=repo, head=head)
        self.assertEqual((code, report["state"], report["omitted"], report["not_reviewable"]), (0, "reviewed", [], ["logo.png"]))

    def test_without_the_head_blob_only_a_binary_name_counts_as_no_text(self):
        files = [source.File("icon.PNG", "", "modified", True), source.File("font.woff2", "", "added", True),
                 source.File("main.go", "", "modified", True)]
        report, code, _, _ = self.run_review(files)
        self.assertEqual((code, report["state"]), (2, "partial"))
        self.assertEqual((report["omitted"], report["not_reviewable"]), (["main.go"], ["font.woff2", "icon.PNG"]))

    def test_what_is_left_out_of_a_review_still_makes_it_partial(self):
        other = source.File("other.py", "@@ -0,0 +1 @@\n+y = 2\n", "added")
        report, code, _, posts = self.run_review([ADDED, other], "--path", "app.py")
        self.assertEqual((code, report["state"]), (2, "partial"))
        self.assertEqual((report["omitted"], report["not_reviewable"]), (["other.py"], []))
        self.assertIn("- Outside review coverage: ` other.py `", posts[0])
        self.assertNotIn("Not reviewed, no text", posts[0])


if __name__ == "__main__":
    unittest.main()
