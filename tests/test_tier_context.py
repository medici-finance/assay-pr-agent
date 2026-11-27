import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cursor_pr import context, discovery, engine
from cursor_pr.config import project_from
from cursor_pr.source import File, local_change


class TierContextTests(unittest.TestCase):
    def test_gitlab_context_excludes_non_public_and_unverified_notes(self):
        project = project_from(dict(provider="gitlab", url="https://gitlab.com/team/repo",
                                    checkout=tempfile.gettempdir(), requester_ids=[1]))
        visible = dict(system=False, internal=False, noteable_type="MergeRequest", noteable_iid=7)
        notes = [dict(visible, body="Public update"), dict(visible, body="Internal", internal=True),
                 dict(visible, body="Confidential", confidential=True),
                 dict(visible, body="System", system=True),
                 dict(visible, body="Different change", noteable_iid=8),
                 dict(visible, body="Visibility unknown", internal=None),
                 dict(visible, body="Earlier public update")]
        response = Mock()
        response.json.return_value = notes
        with patch.dict(os.environ, {"GITLAB_API_BASE": "https://gitlab.com/api/v4"}), \
             patch.object(discovery, "reviewer_token", return_value="fixture"), \
             patch.object(discovery.httpx, "get", return_value=response) as get:
            result = discovery.context_comments(project, 7)
        self.assertEqual([note["body"] for note in result], ["Earlier public update", "Public update"])
        self.assertFalse(get.call_args.kwargs["follow_redirects"])

    def test_blob_preserves_leading_blank_lines_and_indentation_and_grep_is_bounded(self):
        with tempfile.TemporaryDirectory() as path:
            repo = Path(path)
            def git(*args):
                return subprocess.check_output(["git", "-C", path, "-c", "user.name=Test",
                                                "-c", "user.email=test@example.org", *args])
            git("init", "-q")
            text = "\n\n    first = 1\n"
            (repo / "module.py").write_text(text)
            (repo / "large.py").write_text("x" * 2000 + "\n")
            git("add", ".")
            git("commit", "-qm", "fixture")
            head = git("rev-parse", "HEAD").decode().strip()
            self.assertEqual(context.blob(repo, head, "module.py"), text)
            with patch.object(context, "MAX_RAW", 50):
                self.assertEqual(list(context.git_lines(repo, "grep", "-I", "-F", "-e", "x", head)), [])

    def test_surrounding_stops_reading_when_the_context_limit_is_reached(self):
        patch_text = "@@ -1,2 +1,2 @@\n alpha\n-beta_before\n+beta\n"
        files = [File(f"{name}.py", patch_text, "modified") for name in ("one", "two")]
        expected = "## one.py lines 1-2 at the head\n1: alpha\n2: beta"
        with patch.object(context, "MAX_RAW", len(expected) - 2), \
             patch.object(context, "blob", return_value="alpha\nbeta\n") as blob:
            text = context.surrounding(None, "head", files, radius=0)
        self.assertEqual(text, expected[:-2])
        blob.assert_called_once_with(None, "head", "one.py")

    def test_whole_files_stops_reading_when_the_context_limit_is_reached(self):
        files = [File(f"{name}.py", "present", "modified") for name in ("one", "two", "three")]
        with patch.object(context, "MAX_RAW", 45), patch.object(context, "blob", return_value="x" * 40) as blob:
            text = context.whole_files(None, "head", files)
        self.assertEqual(len(text), 45)
        self.assertTrue(text.startswith("## one.py at the head"))
        blob.assert_called_once_with(None, "head", "one.py")

    def test_verification_preserves_distinct_findings_at_the_same_location(self):
        issue = dict(path="app.py", line=1, end_line=1, title="First issue", message="First defect")
        other = dict(issue, title="Other issue", message="Different defect")
        self.assertEqual(engine.merge_findings([issue], [issue, other]), [issue, other])
        self.assertEqual(engine.kept([issue, other], {"findings": [other]}), [other])
        file = File("app.py", "@@ -1 +1 @@\n-foo()\n+bar()\n", "modified")
        self.assertIn("keep the finding unchanged and unverified", engine.verification([issue], [file]))


class CallSiteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.module = "def alpha_fn():\n    return 1\n\n\ndef beta_fn():\n    return 2\n\n\ndef gamma_fn():\n    return 3\n"
        (self.repo / "app.py").write_text(self.module)
        (self.repo / "a:b.py").write_text("x = gamma_fn()\n")
        (self.repo / "users.py").write_text("alpha_fn()\n" * 5 + "beta_fn()\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.git("switch", "-qc", "feature")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.org", *args], text=True).strip()

    def change(self, text):
        (self.repo / "app.py").write_text(text)
        self.git("commit", "-qam", "change")
        head = self.git("rev-parse", "HEAD")
        return head, local_change(self.repo, "main", head).files

    def test_only_names_the_change_touches_are_symbols(self):
        # The hunk header names beta_fn and the hunk shows the def line of gamma_fn; only gamma_fn changes.
        _, files = self.change(self.module.replace("return 3", "return 30"))
        self.assertEqual(context.symbols(files), ["gamma_fn"])

    def test_one_name_does_not_crowd_another_out_of_a_file(self):
        head, files = self.change(self.module.replace("return 1", "return 10"))
        with patch.object(context, "symbols", return_value=["alpha_fn", "beta_fn", "gamma_fn"]):
            text = context.call_sites(self.repo, head, files)
        self.assertIn("## Uses of beta_fn\nusers.py:6: beta_fn()", text)
        self.assertEqual(text.count("users.py:"), 6)
        self.assertIn("## Uses of gamma_fn\na:b.py:1: x = gamma_fn()", text)  # A colon in a path.

    def test_git_failure_is_unavailable_not_empty(self):
        head, files = self.change(self.module.replace("return 3", "return 30"))
        self.assertIsNone(context.call_sites(self.repo, "0" * 40, files))
        self.assertIsNone(context.call_sites(self.repo / "missing", head, files))

    def test_description_without_a_branch_point_is_unavailable(self):
        head, _ = self.change(self.module.replace("return 3", "return 30"))
        change = SimpleNamespace(description="", head=head)
        self.assertIsNone(context.description(change, self.repo, self.repo, ""))
        self.assertEqual(context.description(change, self.repo, self.repo, self.git("rev-parse", "main")), "change")
