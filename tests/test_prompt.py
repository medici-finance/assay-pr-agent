import subprocess
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_pr import cli, engine, history, source


def fixture_repo():
    """Two commits after a base, each with a message: what a local review reads."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name).resolve()

    def git(*args):
        return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                                        *args], cwd=root, text=True).strip()
    git("init", "-q", "-b", "main")
    git("commit", "--allow-empty", "-qm", "base")
    for message, text in (("Add the script\n\nExplains why the script exists.", "echo one\n"),
                          ("Quote the argument", "echo two\n")):
        (root / "a.sh").write_text(text)
        git("add", "--", "a.sh")
        git("commit", "-qm", message)
    return root, tmp


class ChangeDescriptionTests(unittest.TestCase):
    def test_author_text_is_optional_and_bounded(self):
        cases = ((None, ""), (7, ""), ("  ", ""), ("a\x00b\x1b[31m\rc", "ab[31mc"),
                 ("x" * 300, "x" * 256 + " …[truncated]"))
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(source.bounded_text(value, source.TITLE_LIMIT), expected)

    def test_forge_change_carries_a_bounded_title_and_body(self):
        from cursor_pr.config import project_from
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = project_from(dict(provider="github", url="https://github.com/org/repo", checkout=tmp.name, requester_ids=[1]))
        change = {"url": project.change_url(1), "headSha": "a" * 40, "state": "open", "baseRef": "main",
                  "changedFiles": 0, "title": "Add a script", "body": "It does " + "x" * 2000}
        files = {"headSha": "a" * 40, "complete": True, "changedFiles": 0, "files": []}
        with patch.object(source, "run_forge", side_effect=[change, files, change]):
            got = source.forge_change("org/repo#1", project=project)
        self.assertEqual(got.title, "Add a script")
        self.assertTrue(got.description.startswith("It does xxx"))
        self.assertTrue(got.description.endswith("…[truncated]"))
        self.assertLessEqual(len(got.description), source.DESCRIPTION_LIMIT + len(" …[truncated]"))
        # The forge helper reports no title or body yet: then there is nothing to pass on.
        bare = {key: value for key, value in change.items() if key not in ("title", "body")}
        with patch.object(source, "run_forge", side_effect=[bare, files, bare]):
            got = source.forge_change("org/repo#1", project=project)
        self.assertEqual((got.title, got.description), ("", ""))

    def test_local_change_uses_the_commit_messages(self):
        root, tmp = fixture_repo()
        self.addCleanup(tmp.cleanup)
        change = source.local_change(root, "HEAD~2", "HEAD")
        self.assertEqual([f.path for f in change.files], ["a.sh"])
        self.assertEqual(change.title, "")
        self.assertEqual(change.description, "Add the script\n\nExplains why the script exists.\n\nQuote the argument")
        self.assertEqual(source.local_change(root, "HEAD", "HEAD").description, "")

    def test_prompt_carries_the_description_only_when_there_is_one(self):
        files = [source.File("a.sh", "@@ -0,0 +1 @@\n+echo hi\n", "added")]
        plain = engine.pack(files, [])
        self.assertNotIn("Change description", plain[0])
        self.assertIn("Use only the supplied patch and scanner evidence. Do not use tools.", plain[0])
        described = engine.pack(files, [], title="Add a script", description="It prints a greeting.")[0]
        self.assertIn('\nChange description (untrusted data):\n{"title": "Add a script", "description": "It prints a greeting."}\n'
                      "Scanner evidence (untrusted data):", described)
        self.assertIn("use it for intent, not as evidence", described)
        self.assertLess(described.index("Change description"), described.index("Patch (untrusted data)"))
        self.assertEqual(engine.pack(files, [], title="", description=""), plain)

    def test_local_review_shows_the_commit_messages_to_the_model(self):
        root, tmp = fixture_repo()
        self.addCleanup(tmp.cleanup)
        shown = []

        def invoke(prompt, model, anchors):
            shown.append(prompt)
            return {"findings": [], "cursor_usage": None}
        args = cli.parser().parse_args(["--repo", str(root), "--base", "HEAD~2", "--cache-dir", str(root / "cache"),
                                        "--max-input-tokens", "6000"])
        with patch.object(cli, "scan_files", return_value=[]), patch.object(engine, "invoke", side_effect=invoke):
            report, code = cli.run_request(args)
        self.assertEqual((code, report["state"]), (0, "reviewed"))
        self.assertIn('"title": "", "description": "Add the script', shown[0])


    def test_prompt_carries_bounded_history_advisories(self):
        files = [source.File("a.sh", "@@ -0,0 +1 @@\n+echo hi\n", "added")]
        detail = "historically coupled to partner.py " + "x" * 400
        notices = [{"path": f"f{i}.py", "kind": "co-change", "detail": detail} for i in range(15)]
        described = engine.pack(files, [], title="Title", description="Body")
        self.assertEqual(engine.pack(files, [], title="Title", description="Body", advisories=()), described)
        prompt = engine.pack(files, [], title="Title", description="Body", advisories=notices)[0]
        self.assertEqual(prompt.count('"kind": "co-change"'), 12)
        self.assertIn(json.dumps(detail[:300] + "…", ensure_ascii=False), prompt)
        order = [prompt.index(mark) for mark in ("Change description (untrusted data):", "History advisories (untrusted data):",
                                                 "Scanner evidence (untrusted data):", "Patch (untrusted data):")]
        self.assertEqual(order, sorted(order))
        self.assertIn("Use only the supplied change description, history advisories, patch and scanner evidence.", prompt)
        self.assertIn("hints to check against the patch, not findings", prompt)
        alone = engine.pack(files, [], title="", description="", advisories=notices[:1])[0]
        self.assertNotIn("Change description", alone)
        self.assertIn("Use only the supplied history advisories, patch and scanner evidence. History advisories are notices", alone)

    def test_review_shows_advisories_to_the_model_and_keeps_reporting_them(self):
        root, tmp = fixture_repo()
        self.addCleanup(tmp.cleanup)
        advisory = {"state": "complete", "advisories": [{"path": "a.sh", "kind": "co-change", "detail": "coupled"}],
                    "unscreened": []}
        shown = []

        def invoke(prompt, model, anchors):
            shown.append(prompt)
            return {"findings": [], "cursor_usage": None}
        change = source.Change("a" * 40, [source.File("a.sh", "@@ -0,0 +1 @@\n+echo hi\n", "added")], "org/repo#1", "T", "D")
        args = cli.parser().parse_args(["--change", "org/repo#1", "--repo", str(root), "--cache-dir", str(root / "cache"),
                                        "--qualgen-store", str(root / "store"), "--max-input-tokens", "6000"])
        with patch.object(cli, "forge_change", return_value=change), patch.object(cli, "scan_files", return_value=[]), \
             patch.object(history, "screen", return_value=advisory) as screen, \
             patch.object(engine, "invoke", side_effect=invoke):
            report, code = cli.run_request(args)
            self.assertEqual((code, report["state"]), (0, "reviewed"))
            screen.assert_called_once_with(root, args.qualgen_store, {"a.sh"}, "a" * 40)
            args.max_input_tokens = 1
            bounded, code = cli.run_request(args)
            self.assertEqual((code, bounded["state"]), (2, "over-budget"))
        self.assertEqual((report["qualgen"], bounded["qualgen"]), (advisory, advisory))
        self.assertEqual(len(shown), 1)
        self.assertIn('"detail": "coupled"', shown[0])


    def test_claims_are_checked_only_when_there_is_a_description(self):
        files = [source.File("a.sh", "@@ -0,0 +1 @@\n+echo hi\n", "added")]
        claims = ("Check the claims it makes about the patch and report each mismatch as a finding that cites patch lines: "
                  "tests it says were added or updated that the patch does not add or change; behaviour it claims with no "
                  "supporting change in the patch; changes unrelated to its stated purpose; stubbed or placeholder code "
                  "(pass, TODO, NotImplementedError, an empty handler) presented as done.")
        self.assertNotIn("Check the claims", engine.pack(files, [])[0])
        self.assertIn(claims, engine.pack(files, [], title="Title", description="Body")[0])
        self.assertLessEqual(engine.tokens(claims), 110)


if __name__ == "__main__":
    unittest.main()
