import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from cursor_pr import context, discovery, engine
from cursor_pr.config import project_from
from cursor_pr.source import Change, File, base_line, hunks, local_change


class TierContextTests(unittest.TestCase):
    def test_gitlab_context_excludes_non_public_and_unverified_notes(self):
        project = project_from(dict(provider="gitlab", url="https://gitlab.com/team/repo",
                                    checkout=tempfile.gettempdir(), requester_ids=[1]))
        visible = dict(system=False, internal=False, noteable_type="MergeRequest", noteable_iid=7,
                       author={"id": 1, "bot": False})
        notes = [dict(visible, body="Public update"), dict(visible, body="Internal", internal=True),
                 dict(visible, body="Confidential", confidential=True),
                 dict(visible, body="System", system=True),
                 dict(visible, body="Different change", noteable_iid=8),
                 dict(visible, body="Visibility unknown", internal=None),
                 dict(visible, body="Different author", author={"id": 2}),
                 dict(visible, body="Bot author", author={"id": 1, "bot": True}),
                 dict(visible, body="Earlier public update")]
        response = Mock()
        response.json.return_value = notes
        with patch.dict(os.environ, {"GITLAB_API_BASE": "https://gitlab.com/api/v4"}), \
             patch.object(discovery, "reviewer_token", return_value="fixture"), \
             patch.object(discovery.httpx, "get", return_value=response) as get:
            result = discovery.context_comments(project, 7)
        self.assertEqual([note["body"] for note in result], ["Earlier public update", "Public update"])
        self.assertFalse(get.call_args.kwargs["follow_redirects"])

    def test_github_context_requires_a_human_requester_on_this_change(self):
        project = project_from(dict(provider="github", url="https://github.com/org/repo",
                                    checkout=tempfile.gettempdir(), requester_ids=[1]))
        address = project.change_url(7)
        allowed = dict(authorId=1, authorType="User", databaseId=4, url=f"{address}#issuecomment-4",
                       body="Authorized update")
        notes = [allowed, dict(allowed, authorId=2), dict(allowed, authorType="Bot"),
                 dict(allowed, url=f"{project.change_url(8)}#issuecomment-4")]
        with patch.object(discovery, "run_forge", return_value={"items": [{"number": 7, "comments": notes}]}) as run:
            self.assertEqual(discovery.context_comments(project, 7), [allowed])
        run.assert_called_once_with("context-comments", project, numbers=[7], timeout=120)

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

    def test_prior_review_summaries_do_not_displace_human_comments(self):
        notes = [{"body": "Please keep this design note."},
                 *[{"body": f"Codex review of {'a' * 40}\nreviewed\nOld model finding {i}"} for i in range(12)],
                 {"body": "/review-max"}, {"body": "This is a new design note."}]
        context_text = context.earlier_comments(notes)
        self.assertIn("## Comment 1\nPlease keep this design note.", context_text)
        self.assertIn("## Comment 2\nThis is a new design note.", context_text)
        self.assertNotIn("Old model finding", context_text)

    def test_verification_preserves_distinct_findings_at_the_same_location(self):
        issue = dict(path="app.py", line=1, end_line=1, title="First issue", message="First defect")
        other = dict(issue, title="Other issue", message="Different defect")
        self.assertEqual(engine.merge_findings([issue], [issue, other]), [issue, other])
        self.assertEqual(engine.kept([issue, other], {"findings": [other]}),
                         [dict(other, verification_status="unverified")])
        uncertain = dict(issue, verification_status="unverified")
        confirmed = dict(issue, verification_status="confirmed")
        self.assertEqual(engine.kept([uncertain, other], {"findings": [confirmed]}), [confirmed])
        self.assertEqual(engine.kept([confirmed], {"findings": [uncertain]}), [uncertain])
        self.assertEqual(engine.kept([confirmed], {"findings": [issue]}), [uncertain])
        self.assertEqual(engine.kept([confirmed], {"findings": [dict(issue, verification_status="unknown")]}),
                         [uncertain])
        file = File("app.py", "@@ -1 +1 @@\n-foo()\n+bar()\n", "modified")
        self.assertIn("keep the finding unchanged and unverified", engine.verification([issue], [file]))

    def test_verification_tries_smaller_source_excerpts_with_the_same_patch(self):
        patch = "@@ -1,20 +1,20 @@\n-old()\n+new()\n" + "".join(f" line_{n}()\n" for n in range(2, 21))
        file = File("app.py", patch, "modified")
        finding = dict(path="app.py", line=1, end_line=1, title="Example", message="Example defect")

        def comparison(file, start, end, radius):
            return [f"Source excerpt radius {radius} " + "context " * (radius * 8)]

        patch_only = engine.verification([finding], [file], radius=15)
        variants = {radius: engine.verification([finding], [file], radius=15, comparison=comparison,
                                                source_radius=radius) for radius in (15, 8, 3)}
        self.assertIn("16  line_16()", patch_only)
        for expected in (variants[8], variants[3], patch_only):
            with self.subTest(expected=expected.splitlines()[-1]):
                limit = engine.tokens(expected)
                prompt, size = engine.fit_verification([finding], [file], limit, comparison)
                self.assertEqual((prompt, size), (expected, limit))
                self.assertIn("16  line_16()", prompt)

    def test_verification_keeps_the_old_lines_of_a_replacement_at_the_start_of_a_hunk(self):
        issue = dict(path="app.py", line=11, end_line=11, title="Changed behavior", message="Check the replacement.")
        file = File("app.py", "@@ -10,5 +10,5 @@\n-old_first()\n-old_second()\n+new_first()\n+new_second()\n"
                    " context()\n-other_old()\n+other_new()\n tail()\n"
                    "@@ -100 +100 @@\n-distant_old()\n+distant_new()\n", "modified")

        narrow = engine.verification([issue], [file], radius=0)
        self.assertTrue(narrow.endswith(
            "## app.py around lines 11-11\n- -old_first()\n- -old_second()\n11 +new_second()"))
        wide = engine.verification([issue], [file], radius=15)
        self.assertIn("- -old_first()\n- -old_second()\n10 +new_first()\n11 +new_second()", wide)
        self.assertNotIn("distant_old()", wide)

    def test_verification_aligns_base_excerpts_with_large_replacement(self):
        old = [f"old_{number:02}()" for number in range(1, 33)]
        new = [f"new_{number:02}()" for number in range(1, 36)]
        diff = "@@ -1,33 +1,36 @@\n" + "".join(f"-{line}\n" for line in old) + "".join(
            f"+{line}\n" for line in new) + " retained()\n"
        file = File("app.py", diff, "modified")
        patch_rows = hunks(file)
        self.assertIsNone(base_line(patch_rows, 1))
        self.assertEqual([base_line(patch_rows, line, nearest=True) for line in (1, 18, 35, 36)], [1, 18, 32, 33])
        with patch.object(context, "blob", side_effect=["\n".join(old), "\n".join(new)]):
            comparison = context.verification_comparison(Path("."), Path("."), "base", "head")
            base, head = comparison(file, 1, 1, 3)
        self.assertIn("1: old_01()", base)
        self.assertNotIn("old_32()", base)
        self.assertIn("1: new_01()", head)

    def test_verification_keeps_deletion_adjacent_to_the_cited_context_line(self):
        issue = dict(path="app.py", line=2, end_line=2, title="Removed step", message="Check the missing step.")
        file = File("app.py", "@@ -1,3 +1,2 @@\n initialize()\n-check_config()\n finish()\n", "modified")

        prompt = engine.verification([issue], [file], radius=0)
        self.assertTrue(prompt.endswith("## app.py around lines 2-2\n- -check_config()\n2  finish()"))

    def test_narrow_verification_bounds_large_deletions_to_the_nearest_lines(self):
        deleted = "".join(f"-removed_{i:03d}()\n" for i in range(32))
        file = File("app.py", f"@@ -1,34 +1,2 @@\n initialize()\n{deleted} finish()\n", "modified")
        after = dict(path="app.py", line=2, end_line=2, title="Removed step", message="Check the missing step.")
        before = dict(after, line=1, end_line=1)

        narrow_after = engine.verification([after], [file], radius=0)
        self.assertTrue(narrow_after.endswith("[28 removed lines omitted from this window]\n"
                                              "- -removed_028()\n- -removed_029()\n"
                                              "- -removed_030()\n- -removed_031()\n2  finish()"))
        narrow_before = engine.verification([before], [file], radius=0)
        self.assertTrue(narrow_before.endswith("1  initialize()\n"
                                               "- -removed_000()\n- -removed_001()\n"
                                               "- -removed_002()\n- -removed_003()\n"
                                               "[28 removed lines omitted from this window]"))
        both = engine.verification([dict(after, line=1)], [file], radius=0)
        self.assertTrue(both.endswith("1  initialize()\n- -removed_000()\n- -removed_001()\n"
                                      "[28 removed lines omitted from this window]\n"
                                      "- -removed_030()\n- -removed_031()\n2  finish()"))
        wide = engine.verification([after], [file], radius=15)
        self.assertGreater(wide.count("- -removed_"), narrow_after.count("- -removed_"))
        self.assertIn("- -removed_031()\n2  finish()", wide)

    def test_verification_keeps_both_removal_edges_across_a_no_newline_marker(self):
        deleted = "".join(f"-removed_{i}()\n" for i in range(8))
        note = "\\ No newline at end of file"
        file = File("app.py", f"@@ -1,9 +1,2 @@\n initialize()\n{deleted}{note}\n+finish()\n", "modified")
        finding = dict(path="app.py", line=1, end_line=2, title="Removed step", message="Check the missing step.")

        prompt = engine.verification([finding], [file], radius=0)
        self.assertTrue(prompt.endswith("1  initialize()\n- -removed_0()\n- -removed_1()\n"
                                        "[4 removed lines omitted from this window]\n"
                                        "- -removed_6()\n- -removed_7()\n"
                                        f"- {note}\n2 +finish()"))

    def test_narrow_verification_marks_a_shortened_removed_line(self):
        deleted = "x" * 5000
        file = File("app.py", f"@@ -1,2 +1 @@\n-{deleted}\n finish()\n", "modified")
        issue = dict(path="app.py", line=1, end_line=1, title="Removed step", message="Check the missing step.")

        narrow = engine.verification([issue], [file], radius=0)
        wide = engine.verification([issue], [file], radius=15)
        self.assertIn(" [remaining text omitted]\n1  finish()", narrow)
        self.assertNotIn(deleted, narrow)
        self.assertLess(len(narrow), len(wide))


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

    def test_renamed_and_removed_definitions_keep_their_old_caller_names(self):
        head, files = self.change(self.module.replace("def gamma_fn", "def renamed_fn"))
        self.assertEqual(context.symbols(files), ["gamma_fn", "renamed_fn"])
        self.assertIn("## Uses of gamma_fn\na:b.py:1: x = gamma_fn()", context.call_sites(self.repo, head, files))

        self.git("rm", "app.py")
        self.git("commit", "-qm", "remove module")
        deleted = local_change(self.repo, "main", "HEAD").files
        self.assertIn("gamma_fn", context.symbols(deleted))

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
        change = Change(head, [], str(self.repo))
        self.assertIsNone(context.description(change, self.repo, self.repo, ""))
        self.assertEqual(context.description(change, self.repo, self.repo, self.git("rev-parse", "main")), "change")
        described = Change(head, [], str(self.repo), title="Review title", description="Review body")
        self.assertEqual(context.description(described, self.repo, self.repo, ""), "Review title\n\nReview body")
