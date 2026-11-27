import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import cli, drift
from cursor_pr.source import Change, File, local_change


class DriftTests(unittest.TestCase):
    """main removes `oldtool` and edits shared.py after the feature branch quoted and edited them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name)
        self.root = self.top / "origin"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.write("tool.py", "def oldtool():\n    return 1\n")
        self.write("shared.py", "VALUE = 1\n")
        self.write("README.md", "# Project\n")
        self.commit("base")
        self.fork = self.git("rev-parse", "HEAD")
        self.git("switch", "-qc", "feature")
        self.write("README.md", "# Project\n\nRun `oldtool sync --fast`, then `git status` and `newthing`.\n"
                                "Plain oldtool prose and `VALUE` are not stale.\n")
        self.write("shared.py", "VALUE = 2\n")
        self.write("notes.py", "# `oldtool` in code is the model's to judge\n")
        self.commit("feature")
        self.head = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "main")

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                                        *args], cwd=cwd or self.root, text=True).strip()

    def write(self, path, text):
        (self.root / path).write_text(text)

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)

    def move_main(self):
        self.write("tool.py", "def replacement():\n    return 1\n")
        self.commit("rename the tool")
        self.write("shared.py", "VALUE = 3\n")
        self.commit("edit the shared file")
        return self.git("rev-parse", "HEAD")

    def files(self):
        return local_change(self.root, "main", self.head).files

    def test_reports_commits_behind_shared_files_and_names_removed_on_the_base(self):
        tip = self.move_main()
        result = drift.check(self.root, self.head, "main", self.files())
        self.assertEqual(result, {"state": "complete", "base": tip, "fork": self.fork, "behind": 2, "both": ["shared.py"],
                                  "references": [{"path": "README.md", "line": 3, "name": "oldtool"}]})
        self.assertEqual(drift.summary(result), [
            f"Base drift (base {tip[:12]}): the head is 2 commits behind.",
            "- Changed on both sides since the branch point: ` shared.py `",
            "- ` README.md `:3 — ` oldtool ` is quoted here but no longer appears on the base."])

    def test_renamed_file_whose_old_path_changed_on_the_base_is_on_both_sides(self):
        self.move_main()
        renamed = File("moved.py", "", "renamed", True, "shared.py")
        self.assertEqual(drift.check(self.root, self.head, "main", [renamed])["both"], ["shared.py"])

    def test_forge_drift_covers_only_the_selected_files(self):
        self.move_main()
        change = Change(self.head, self.files(), "url", base_ref="main")
        readme = [f for f in change.files if f.path == "README.md"]
        self.assertEqual(drift.forge(self.root, self.root, change, False)["both"], ["shared.py"])
        self.assertEqual(drift.forge(self.root, self.root, change, False, readme)["both"], [])

    def test_name_still_on_the_base_or_never_in_the_tree_is_not_reported(self):
        self.write("unrelated.py", "x = 1\n")
        self.commit("main moves without touching the tool")
        result = drift.check(self.root, self.head, "main", self.files())
        self.assertEqual((result["state"], result["behind"], result["both"], result["references"]),
                         ("complete", 1, [], []))

    def test_name_only_inside_a_longer_quoted_name_is_not_reported(self):
        self.write("cli.py", "# run drift-check\n")
        self.commit("add the command")
        self.git("switch", "-qc", "overlap")
        self.write("README.md", "# Project\n\nUse `drift-check`, which runs `drift`.\n")
        self.commit("quote both names")
        head = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "main")
        self.write("unrelated.py", "x = 1\n")
        self.commit("main moves without touching the command")
        result = drift.check(self.root, head, "main", local_change(self.root, "main", head).files)
        self.assertEqual((result["state"], result["behind"], result["references"]), ("complete", 1, []))

    def test_head_that_contains_the_base_tip_has_no_drift(self):
        result = drift.check(self.root, self.head, "main", self.files())
        self.assertEqual((result["state"], result["behind"], result["references"]), ("complete", 0, []))
        self.assertEqual(drift.summary(result),
                         [f"Base drift (base {self.fork[:12]}): none, the head contains the base tip."])

    def test_unknown_base_or_missing_head_is_could_not_check(self):
        self.move_main()
        for head, base in ((self.head, "no-such-branch"), ("a" * 40, "main")):
            result = drift.check(self.root, head, base, self.files())
            self.assertEqual((result["state"], result["behind"]), ("could-not-check", None))
            self.assertEqual(drift.summary(result), ["Base drift could not be checked."])

    def test_forge_change_whose_head_is_in_a_separate_checkout(self):
        tip = self.move_main()
        self.git("update-ref", "refs/pull/7/head", self.head)
        # Like a managed checkout: the default branch only, and no named branch refs.
        managed = self.top / "managed"
        self.git("clone", "-q", "--single-branch", "--branch", "main", self.root.as_uri(), str(managed), cwd=self.top)
        self.git("checkout", "-q", "--detach", cwd=managed)
        self.git("update-ref", "-d", "refs/remotes/origin/main", cwd=managed)
        self.git("update-ref", "-d", "refs/heads/main", cwd=managed)
        change = Change(self.head, self.files(), "url", base_ref="main")
        self.assertEqual(drift.forge(managed, managed, change, True)["state"], "could-not-check")  # No head here.
        for depth, state in ((50, "complete"), (1, "could-not-check")):
            with self.subTest(depth=depth):
                fetched = self.top / f"fetched-{depth}"
                self.git("init", "-q", str(fetched), cwd=self.top)
                self.git("fetch", "-q", f"--depth={depth}", self.root.as_uri(), "refs/pull/7/head", cwd=fetched)
                self.git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=fetched)
                result = drift.forge(managed, fetched, change, True)
                self.assertEqual(result["state"], state)
                if state == "complete":
                    self.assertEqual((result["base"], result["behind"], result["both"],
                                      [row["name"] for row in result["references"]]),
                                     (tip, 2, ["shared.py"], ["oldtool"]))
        # An unmanaged checkout must name the base branch; its HEAD is not assumed to be the base.
        self.assertEqual(drift.forge(managed, managed, change, False)["state"], "could-not-check")
        hostile = Change(self.head, self.files(), "url", base_ref="main..feature")
        self.assertEqual(drift.forge(self.root, self.root, hostile, False)["state"], "could-not-check")
        self.assertEqual(drift.forge(self.root, self.root, change, False)["behind"], 2)

    def test_review_report_and_posted_comment_carry_the_drift(self):
        tip = self.move_main()
        argv = ["cursor-review", "--repo", str(self.root), "--base", "main", "--head", self.head, "--estimate"]
        out = io.StringIO()
        with patch("sys.argv", argv), patch.dict(os.environ, {"REVIEW_STATE_DIR": str(self.top / "state")}), \
             contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(), 0)
        report = json.loads(out.getvalue())
        self.assertEqual((report["drift"]["base"], report["drift"]["behind"]), (tip, 2))
        report.update(state="reviewed", provider="cursor")
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", self.head, report)
        self.assertIn("- ` README.md `:3 — ` oldtool ` is quoted here but no longer appears on the base.",
                      post.call_args.args[3].splitlines())


if __name__ == "__main__":
    unittest.main()
