import subprocess
import sys
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
    def run_review(self, files, *flags, repo=None, head="a" * 40, base=""):
        shown, posts = [], []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        args = cli.parser().parse_args(["--change", "org/repo#1", "--repo", repo or tmp.name, "--cache-dir", str(Path(tmp.name) / "cache"),
                                        "--max-input-tokens", "6000", *flags])

        def invoke(prompt, model, anchors, **kwargs):
            shown.append(prompt)
            return {"findings": [], "dropped": [], "cursor_usage": None}
        with patch.object(cli, "forge_change", return_value=source.Change(head, files, "org/repo#1", patch_base=base)), \
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
                                     "empty.py": b""})
        files = [source.File("big.py", "", "modified", True), source.File("blob.dat.txt", "", "added", True),
                 source.File("empty.py", "", "added", True)]
        report, code, shown, posts = self.run_review(files, repo=repo, head=head)
        self.assertEqual((code, report["state"], shown), (2, "partial", []))
        self.assertEqual(report["omitted"], ["big.py"])
        self.assertEqual(report["not_reviewable"], ["blob.dat.txt", "empty.py"])
        self.assertIn("- Outside review coverage: ` big.py `", posts[0])

    def test_binary_files_alone_are_not_partial(self):
        repo, head = self.head_repo({"logo.png": b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR", "app.py": b"x = 1\n"})
        report, code, _, _ = self.run_review([source.File("logo.png", "", "modified", True), ADDED], repo=repo, head=head)
        self.assertEqual((code, report["state"], report["omitted"], report["not_reviewable"]), (0, "reviewed", [], ["logo.png"]))

    def test_patchless_rename_requires_an_unchanged_blob(self):
        repo, base = self.head_repo({"old.py": b"x = 1\n", "old_edited.py": b"y = 1\n"})
        root = Path(repo)
        (root / "old.py").rename(root / "moved.py")
        (root / "old_edited.py").rename(root / "edited.py")
        (root / "edited.py").write_bytes(b"y = 2\n")
        source.git(root, "add", ".")
        source.git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "renames")
        head = source.commit(root, "HEAD")
        files = [source.File("moved.py", "", "renamed", True, previous="old.py"),
                 source.File("edited.py", "", "renamed", True, previous="old_edited.py")]
        report, code, _, _ = self.run_review(files, repo=repo, head=head, base=base)
        self.assertEqual((code, report["state"]), (2, "partial"))
        self.assertEqual((report["not_reviewable"], report["omitted"]), (["moved.py"], ["edited.py"]))

    def test_without_the_head_blob_even_a_binary_suffix_is_unavailable(self):
        files = [source.File("records.dat", "", "modified", True), source.File("icon.PNG", "", "added", True)]
        report, code, _, _ = self.run_review(files)
        self.assertEqual((code, report["state"]), (2, "partial"))
        self.assertEqual((report["omitted"], report["not_reviewable"]), (["icon.PNG", "records.dat"], []))

    def test_unsuccessful_short_blob_read_is_unavailable(self):
        file = source.File("broken.dat", "", "modified", True)
        popen = subprocess.Popen
        for output in (b"", b"\0partial"):
            command = [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({output!r}); sys.exit(9)"]
            with self.subTest(output=output), patch.object(source.subprocess, "Popen",
                                                          side_effect=lambda *args, **kw: popen(command, **kw)):
                files = source.classify_absent(Path("."), "a" * 40, [file], base_root=Path("."), base="")
            self.assertTrue(files[0].patch_unavailable)

    def test_committed_diff_attribute_can_make_a_nul_blob_reviewable(self):
        repo, head = self.head_repo({".gitattributes": b"forced.dat diff\n",
                                     "forced.dat": b"x\0y\n", "ordinary.dat": b"x\0y\n"})
        (Path(repo) / ".gitattributes").write_text("forced.dat -diff\n")
        files = [source.File(name, "", "modified", True) for name in ("forced.dat", "ordinary.dat")]
        report, code, _, _ = self.run_review(files, repo=repo, head=head)
        self.assertEqual((code, report["state"]), (2, "partial"))
        self.assertEqual((report["omitted"], report["not_reviewable"]), (["forced.dat"], ["ordinary.dat"]))

    def test_full_prefix_is_discarded_when_the_command_already_failed(self):
        command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\x00'); sys.exit(9)"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE)
        self.assertEqual(process.wait(timeout=5), 9)
        with patch.object(source.subprocess, "Popen", return_value=process):
            self.assertIsNone(source.command_prefix(command, 2))

    def test_estimate_marks_an_unavailable_patch_incomplete(self):
        missing = source.File("app.py", "", "modified", True)
        report, code, shown, _ = self.run_review([missing], "--estimate")
        self.assertEqual((code, report["state"], shown, report["omitted"]), (2, "estimate", [], ["app.py"]))

    def test_what_is_left_out_of_a_review_still_makes_it_partial(self):
        other = source.File("other.py", "@@ -0,0 +1 @@\n+y = 2\n", "added")
        report, code, _, posts = self.run_review([ADDED, other], "--path", "app.py")
        self.assertEqual((code, report["state"]), (2, "partial"))
        self.assertEqual((report["omitted"], report["not_reviewable"]), (["other.py"], []))
        self.assertIn("- Outside review coverage: ` other.py `", posts[0])
        self.assertNotIn("Not reviewed, no text", posts[0])


if __name__ == "__main__":
    unittest.main()
