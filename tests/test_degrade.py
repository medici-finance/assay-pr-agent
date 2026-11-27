import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import cli, engine, source
from cursor_pr.config import project_from

FILES = {"src/app.py": "def run(argument):\n    return argument\n",
         "tests/test_app.py": "".join(f"def test_case_{n}():\n    assert run({n}) == {n}\n" for n in range(120)),
         "docs/guide.md": "".join(f"Step {n}: run the tool again.\n" for n in range(60)),
         "uv.lock": "".join(f'name = "package-{n}"\n' for n in range(30))}


class DegradeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("commit", "--allow-empty", "-qm", "base")
        for path, text in FILES.items():
            (self.root / path).parent.mkdir(exist_ok=True)
            (self.root / path).write_text(text)
        self.git("add", ".")
        self.git("commit", "-qm", "change")
        self.head = self.git("rev-parse", "HEAD")
        self.prompts = self.root / "prompts"
        issue = {"relevant_file": "src/app.py", "start_line": 2, "end_line": 2,
                 "issue_header": "Identity", "issue_content": "Returns its argument."}
        envelope = {"type": "result", "subtype": "success", "is_error": False,
                    "result": json.dumps({"review": {"key_issues_to_review": [issue]}})}
        shim = self.root / "cursor-agent"
        shim.write_text("#!/usr/bin/env python3\nimport sys\n"
                        f"open({str(self.prompts)!r}, 'a').write(sys.stdin.read() + '\\0')\n"
                        f"print({json.dumps(envelope)!r})\n")
        shim.chmod(0o700)
        env = patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                                      "GH_TOKEN": "fixture", "REVIEW_STATE_DIR": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("REVIEW_MAX_INPUT_TOKENS", None)

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                                        *args], cwd=self.root, text=True).strip()

    def check(self, *args):
        argv = ["cursor-review", "--repo", str(self.root), "--cache-dir", str(self.root / "cache"), *args]
        out = io.StringIO()
        with patch("sys.argv", argv), contextlib.redirect_stdout(out):
            code = cli.main()
        return code, json.loads(out.getvalue())

    def estimate(self, *paths):
        args = [arg for path in paths for arg in ("--path", path)]
        return self.check("--base", "HEAD~1", "--estimate", "--max-input-tokens", "1000000", *args)[1]

    def test_files_leave_in_a_fixed_order(self):
        self.assertEqual([cli.rank(p) for p in ("uv.lock", "go.sum", "web/app.min.js", "tests/test_app.py",
                                                "pkg/app_test.go", "ui/app.spec.ts", "README.md", "docs/a.png",
                                                "src/app.py", "src/contest.py", "src/latest/app.py")],
                         [0, 0, 0, 1, 1, 1, 2, 2, 3, 3, 3])

    def test_oversized_review_drops_low_value_files_and_says_so(self):
        full = self.estimate()["estimated_input_tokens"]
        code, lock_only = self.check("--base", "HEAD~1", "--estimate", "--max-input-tokens", str(full - 1))
        self.assertEqual((code, lock_only["state"], lock_only["degraded"]), (2, "estimate", ["uv.lock"]))
        self.assertFalse(self.prompts.exists())

        limit = self.estimate("src/app.py")["estimated_input_tokens"]
        code, report = self.check("--base", "HEAD~1", "--max-input-tokens", str(limit))
        self.assertEqual((code, report["state"]), (2, "degraded"))
        self.assertEqual(report["degraded"], ["docs/guide.md", "tests/test_app.py", "uv.lock"])
        self.assertEqual((report["full_input_tokens"], report["estimated_input_tokens"]), (full, limit))
        self.assertEqual(report["omitted"], [])
        self.assertEqual([(f["path"], f["line"]) for f in report["findings"]], [("src/app.py", 2)])
        prompt, = self.prompts.read_text().split("\0")[:-1]  # One model call, for the reduced prompt.
        self.assertIn("def run(argument)", prompt)
        for left_out in ("test_case_1", "Step 1:", "package-1"):
            self.assertNotIn(left_out, prompt)

    def test_nothing_is_dropped_when_no_file_fits_or_the_prompt_fits(self):
        code, report = self.check("--base", "HEAD~1", "--max-input-tokens", "1")
        self.assertEqual((code, report["state"], report["degraded"]), (2, "over-budget", []))
        self.assertFalse(self.prompts.exists())
        code, report = self.check("--base", "HEAD~1", "--max-input-tokens", "1000000")
        self.assertEqual((code, report["state"], report["degraded"]), (0, "reviewed", []))

    def test_file_too_large_to_fit_alone_leaves_before_files_that_fit(self):
        (self.root / "src/big.py").write_text("".join(f"VALUE_{n} = {n}  # a long generated constant\n" for n in range(400)))
        self.git("add", ".")
        self.git("commit", "-qm", "large source file")
        rest = [arg for path in ("tests/test_app.py", "docs/guide.md", "uv.lock") for arg in ("--path", path)]
        # src/big.py is the only source file in scope and is over the limit alone; the rest fit.
        limit = self.check("--base", "HEAD~2", "--estimate", "--max-input-tokens", "1000000",
                           *rest)[1]["estimated_input_tokens"]
        code, report = self.check("--base", "HEAD~2", "--estimate", "--max-input-tokens", str(limit),
                                  "--path", "src/big.py", *rest)
        self.assertEqual((code, report["state"]), (2, "estimate"))
        self.assertIn("src/big.py", report["degraded"])
        self.assertLessEqual(report["estimated_input_tokens"], limit)

    def test_file_that_cannot_fit_with_its_scanner_evidence_leaves_first(self):
        files = source.local_change(self.root, "HEAD~1", self.head).files
        app, guide = (next(file for file in files if file.path == path)
                      for path in ("src/app.py", "docs/guide.md"))
        app_finding = {"path": app.path, "line": 2, "message": "Long scanner details. " * 900}
        guide_finding = {"path": guide.path, "line": 1, "message": "Guide finding"}
        checks = [{"findings": [app_finding, guide_finding]}]
        limit = cli.tokens(cli.pack([guide], checks)[0])
        with patch.object(engine, "section", wraps=engine.section) as section, \
             patch.object(cli, "pack", wraps=cli.pack) as pack:
            (prompt, anchors, _, has_text), estimated, _, dropped = cli.fit([app, guide], checks, limit)
        self.assertEqual([call.args[0].path for call in section.call_args_list], [app.path, guide.path])
        single_file_checks = [(files[0].path, file_checks[0]["findings"])
                              for files, file_checks, *_ in (call.args for call in pack.call_args_list)
                              if len(files) == 1]
        self.assertEqual(single_file_checks, [(app.path, [app_finding]), (guide.path, [guide_finding])])
        self.assertIn("Guide finding", prompt)
        self.assertNotIn("Long scanner details", prompt)
        self.assertTrue(has_text)
        self.assertLessEqual(estimated, limit)
        self.assertEqual(dropped, [app.path])
        self.assertTrue(anchors[guide.path])

    def test_posted_comment_is_marked_degraded_and_lists_the_unreviewed_files(self):
        project = project_from(dict(provider="github", url="https://github.com/org/repo",
                                    checkout=str(self.root), requester_ids=[1]))
        rows = [{"filename": path, "patchAbsent": False, "status": "added",
                 "patch": f"@@ -0,0 +1,{text.count(chr(10))} @@\n" + "".join("+" + line for line in text.splitlines(True))}
                for path, text in FILES.items()]
        files = {"headSha": self.head, "complete": True, "changedFiles": len(rows), "files": rows}
        detail = {"url": project.change_url(1), "headSha": self.head, "state": "open", "baseRef": "main",
                  "changedFiles": len(rows)}
        limit = self.estimate("src/app.py")["estimated_input_tokens"] + 5
        with patch.object(cli, "load_config", return_value={project.key: project}), \
             patch.object(cli, "forge_credentials"), \
             patch.object(source, "run_forge", side_effect=[detail, files, detail]), \
             patch.object(cli, "post_comment") as post:
            code, report = self.check("--change", "org/repo#1", "--post", "--max-input-tokens", str(limit))
        self.assertEqual((code, report["state"], report["publication"]), (2, "degraded", "posted-or-already-present"))
        lines = post.call_args.args[3].splitlines()
        self.assertEqual(lines[1], "degraded")
        self.assertRegex(lines[2], rf"^Degraded review: the full prompt is an estimated \d+ tokens, over the limit "
                                   rf"of {limit}\. Cursor did not review these files:$")
        self.assertEqual(lines[3:6], ["- Not reviewed (token limit): ` docs/guide.md `",
                                      "- Not reviewed (token limit): ` tests/test_app.py `",
                                      "- Not reviewed (token limit): ` uv.lock `"])
        self.assertIn("- ` src/app.py `:2 — ` Identity `. ` Returns its argument. `", lines)

    def test_forge_review_checks_drift_for_the_selected_files_only(self):
        project = project_from(dict(provider="github", url="https://github.com/org/repo",
                                    checkout=str(self.root), requester_ids=[1]))
        rows = [{"filename": path, "patchAbsent": False, "status": "added",
                 "patch": f"@@ -0,0 +1,{text.count(chr(10))} @@\n" + "".join("+" + line for line in text.splitlines(True))}
                for path, text in FILES.items()]
        files = {"headSha": self.head, "complete": True, "changedFiles": len(rows), "files": rows}
        detail = {"url": project.change_url(1), "headSha": self.head, "state": "open", "baseRef": "main",
                  "changedFiles": len(rows)}
        with patch.object(cli, "load_config", return_value={project.key: project}), \
             patch.object(cli, "forge_credentials"), \
             patch.object(source, "run_forge", side_effect=[detail, files, detail]), \
             patch.object(cli.drift, "forge", wraps=cli.drift.forge) as forge:
            self.check("--change", "org/repo#1", "--estimate", "--path", "src/app.py")
        self.assertEqual([f.path for f in forge.call_args.args[4]], ["src/app.py"])

    def test_long_list_of_unreviewed_files_is_capped_with_a_count(self):
        report = {"provider": "cursor", "state": "degraded", "findings": [], "checks": [], "omitted": [],
                  "full_input_tokens": 9000, "max_input_tokens": 6000, "degraded": [f"t/{n:03}.py" for n in range(53)]}
        with patch.object(cli, "post_comment") as post:
            cli.publish("org/repo#1", self.head, report)
        lines = [line for line in post.call_args.args[3].splitlines() if line.startswith("- Not reviewed")]
        self.assertEqual(len(lines), 51)
        self.assertEqual(lines[-1], "- Not reviewed (token limit): 3 more files")


if __name__ == "__main__":
    unittest.main()
