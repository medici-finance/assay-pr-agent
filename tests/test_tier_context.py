import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from cursor_pr import context, discovery, engine
from cursor_pr.config import project_from
from cursor_pr.source import File


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

    def test_verification_preserves_distinct_findings_at_the_same_location(self):
        issue = dict(path="app.py", line=1, end_line=1, title="First issue", message="First defect")
        other = dict(issue, title="Other issue", message="Different defect")
        self.assertEqual(engine.merge_findings([issue], [issue, other]), [issue, other])
        self.assertEqual(engine.kept([issue, other], {"findings": [other]}), [other])
        file = File("app.py", "@@ -1 +1 @@\n-foo()\n+bar()\n", "modified")
        self.assertIn("keep the finding unchanged and unverified", engine.verification([issue], [file]))
