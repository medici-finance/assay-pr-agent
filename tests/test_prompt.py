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
            return {"findings": [], "model_usage": None}
        args = cli.parser().parse_args(["--repo", str(root), "--base", "HEAD~2", "--cache-dir", str(root / "cache"),
                                        "--max-input-tokens", "6000"])
        with patch.object(cli, "scan_files", return_value=[]), patch.object(engine, "invoke", side_effect=invoke):
            report, code = cli.run_request(args)
        self.assertEqual((code, report["state"]), (0, "reviewed"))
        self.assertIn('"title": "", "description": "Add the script', shown[0])
        self.assertIn("Commit messages (untrusted data)", shown[0])
        self.assertNotIn("Check its claims", shown[0])


    def test_prompt_carries_bounded_history_advisories(self):
        files = [source.File("a.sh", "@@ -0,0 +1 @@\n+echo hi\n", "added")]
        detail = "historically coupled to partner.py " + "x" * 400
        notices = [{"path": "not-shown.py", "kind": "co-change", "detail": "skip"}] * 12
        notices += [{"path": "a.sh", "kind": "co-change", "detail": detail} for _ in range(15)]
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
        alone = engine.pack(files, [], title="", description="", advisories=notices[-1:])[0]
        self.assertNotIn("Change description", alone)
        self.assertIn("Use only the supplied history advisories, patch and scanner evidence. History advisories are notices", alone)

    def test_fit_counts_the_advisories_when_deciding_which_file_cannot_fit(self):
        def added(path, lines):
            return source.File(path, f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines), "added")
        big = added("src/big.py", [f"VALUE_{n} = {n}" for n in range(40)])
        small = added("tests/test_a.py", [f"assert {n}" for n in range(5)])
        files = [big, small]
        notices = [{"path": "src/big.py", "kind": "co-change", "detail": "word " * 10} for _ in range(6)]
        bare_big = engine.tokens(engine.pack([big], [])[0])
        bare_small = engine.tokens(engine.pack([small], [])[0])
        advised_big = engine.tokens(engine.pack([big], [], advisories=notices)[0])
        limit = max(bare_big, bare_small)
        self.assertLess(limit, advised_big)
        (prompt, *_), estimated, _, degraded = cli.fit(files, [], limit, advisories=notices)
        self.assertEqual(degraded, [big.path])
        self.assertEqual(estimated, bare_small)
        self.assertNotIn("History advisories", prompt)

    def test_advisories_are_kept_only_for_files_with_shown_lines(self):
        shown = source.File("shown.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")
        skipped = source.File("skipped.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")
        absent = source.File("absent.py", "", "added", absent=True)
        notices = [{"path": f.path, "kind": "co-change", "detail": f"Notice for {f.path}"}
                   for f in [shown, skipped, absent]]
        prompt = engine.pack([shown, skipped, absent], [], frozenset({skipped.path}), advisories=notices)[0]
        self.assertIn("Notice for shown.py", prompt)
        self.assertNotIn("Notice for skipped.py", prompt)
        self.assertNotIn("Notice for absent.py", prompt)

    def test_review_shows_advisories_to_the_model_and_keeps_reporting_them(self):
        root, tmp = fixture_repo()
        self.addCleanup(tmp.cleanup)
        advisory = {"state": "complete", "advisories": [{"path": "a.sh", "kind": "co-change", "detail": "coupled"}],
                    "unscreened": []}
        shown = []

        def invoke(prompt, model, anchors):
            shown.append(prompt)
            return {"findings": [], "model_usage": None}
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
        claims = ("Check its claims against the patch and cite patch lines for each reported mismatch. "
                  f"Prioritize material mismatches alongside code defects within the shared limit of {engine.MAX_FINDINGS} findings. "
                  "Look for claimed test or behaviour changes that are missing, code unrelated to the "
                  "stated purpose, and placeholder code presented as done.")
        self.assertNotIn("Check its claims", engine.pack(files, [])[0])
        self.assertIn(claims, engine.pack(files, [], title="Title", description="Body")[0])
        self.assertLessEqual(engine.tokens(claims), 110)

    def test_claims_require_contradictory_evidence_when_coverage_is_partial(self):
        shown = source.File("a.sh", "@@ -0,0 +1 @@\n+echo hi\n", "added")
        tests = source.File("tests/test_a.py", "@@ -0,0 +1,20 @@\n" + "".join(f"+assert {n} == {n}\n" for n in range(20)),
                            "added")
        caveat = "Report a mismatch only when visible patch lines contradict a claim."
        body = dict(title="Add a greeting", description="Adds tests for the greeting.")
        self.assertNotIn(caveat, engine.pack([shown, tests], [], **body)[0])
        self.assertNotIn(caveat, engine.pack([shown, tests], [], frozenset({tests.path}))[0])
        for case, prompt in (
                ("left out for the budget", engine.pack([shown, tests], [], frozenset({tests.path}), **body)[0]),
                ("no reviewable patch", engine.pack([shown, source.File(tests.path, "", "added", True)], [], **body)[0]),
                ("outside the selected paths", engine.pack([shown], [], scoped=True, **body)[0])):
            with self.subTest(case=case):
                self.assertIn(caveat, prompt)
                self.assertNotIn("claimed test or behaviour changes that are missing", prompt)
        # A degraded review drops test files first: the prompt it sends carries the caveat.
        limit = engine.tokens(engine.pack([shown, tests], [], frozenset({tests.path}), **body)[0])
        self.assertGreater(engine.tokens(engine.pack([shown, tests], [], **body)[0]), limit)
        (prompt, *_), _, _, degraded = cli.fit([shown, tests], [], limit, **body)
        self.assertEqual(degraded, [tests.path])
        self.assertIn(caveat, prompt)


if __name__ == "__main__":
    unittest.main()
