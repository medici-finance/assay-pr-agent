import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_pr import cli, engine, source


def fixture_repo():
    """Two commits after a base, each with a message: what a local review reads."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)

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

    def test_local_description_caps_commit_count_and_message_size(self):
        root, tmp = fixture_repo()
        self.addCleanup(tmp.cleanup)
        base = source.commit(root, "HEAD~2")
        head = source.commit(root, "HEAD")
        with patch.object(source, "DESCRIPTION_COMMITS", 1):
            self.assertEqual(source.local_description(root, base, head), "Quote the argument" + source.TRUNCATED)

        message = root / "message"
        message.write_text("Bounded message\n\n" + "payload " * 20_000)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "--allow-empty",
                        "-q", "-F", str(message)], cwd=root, check=True)
        description = source.local_description(root, head, source.commit(root, "HEAD"))
        self.assertTrue(description.startswith("Bounded message\n\npayload"))
        self.assertTrue(description.endswith(source.TRUNCATED))
        self.assertEqual(len(description), source.DESCRIPTION_LIMIT + len(source.TRUNCATED))

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

    def test_fit_counts_the_description_when_deciding_which_file_cannot_fit(self):
        def added(path, lines):
            return source.File(path, f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines), "added")
        big = added("src/big.py", [f"VALUE_{n} = {n}" for n in range(40)])
        small = added("tests/test_a.py", [f"assert {n}" for n in range(5)])
        files, description = [big, small], "word " * 290
        everything = frozenset({big.path, small.path})
        bare = engine.tokens(engine.pack(files, [], everything)[0])
        described = engine.tokens(engine.pack(files, [], everything, "", description)[0])
        size = engine.tokens(engine.section(big)[0])
        limit = max(bare + size, engine.tokens(engine.pack(files, [], frozenset({big.path}), "", description)[0]))
        # src/big.py fits alone without the description but not with it, and the test file fits.
        self.assertLess(limit, described + size)
        _, estimated, _, degraded = cli.fit(files, [], limit, description=description)
        self.assertEqual(degraded, ["src/big.py"])
        self.assertLessEqual(estimated, limit)

    def test_fit_retries_without_metadata_before_refusing_the_review(self):
        files = [source.File("a.sh", "@@ -0,0 +1 @@\n+echo hi\n", "added")]
        limit = engine.tokens(engine.pack(files, [])[0])
        (prompt, _, _, has_text), estimated, full, degraded = cli.fit(
            files, [], limit, title="Add a script", description="word " * 290)
        self.assertTrue(has_text)
        self.assertNotIn("Change description (untrusted data)", prompt)
        self.assertEqual((estimated, degraded), (limit, []))
        self.assertGreater(full, limit)

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


if __name__ == "__main__":
    unittest.main()
