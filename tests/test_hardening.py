import os
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import patch

from cursor_pr import cli, source
from cursor_pr.scanners import fence


def outside_code_spans(text):
    """Drop the spans Markdown renders as code; only what remains is live Markdown."""
    return re.sub(r"(`+).*?\1", "", text, flags=re.S)


class FenceTests(unittest.TestCase):
    def test_marks_grow_with_the_longest_inner_run_and_newlines_collapse(self):
        self.assertEqual(fence("plain"), "` plain `")
        self.assertEqual(fence("a ` b"), "`` a ` b ``")
        self.assertEqual(fence("a `` b"), "``` a `` b ```")
        self.assertEqual(fence("one\r\ntwo\u2028three"), "` one two three `")
        self.assertEqual(fence(""), "")
        self.assertEqual(fence("x" * 10, 5), "` xxxxx `")
        # Measured after truncation, so a cut run can never close the fence.
        self.assertEqual(fence("```abc", 1), "`` ` ``")


class PublishFenceTests(unittest.TestCase):
    def test_attacker_controlled_text_is_quoted_as_code(self):
        hostile = "@all [click](https://evil.example) `x`"
        report = {"provider": "cursor", "state": "complete",
                  "findings": [{"path": "@all", "line": 1, "title": hostile,
                                "message": "[link](https://evil.example)"}],
                  "checks": [
                      {"tool": "hadolint", "state": "could-not-check", "path": "@all/Dockerfile", "findings": [
                          {"path": "Dockerfile", "line": 3, "rule": "DL3007", "message": "uses @all tag"}]},
                      {"tool": "zizmor", "state": "could-not-check", "reason": "no @all artifact", "findings": []},
                  ],
                  "omitted": ["@all", "[ok](https://evil.example)"]}
        bodies = []
        with patch.object(cli, "run_desk", side_effect=lambda argv: bodies.append(Path(argv[-1]).read_text())):
            cli.publish("org/repo#7", "a" * 40, report)
        body = bodies[0]
        self.assertIn("- ` @all `:1 — `` @all [click](https://evil.example) `x` ``. ` [link](https://evil.example) `", body)
        self.assertIn("- hadolint: could not check ` @all/Dockerfile `", body)
        self.assertIn("- ` Dockerfile `:3 — ` DL3007 ` ` uses @all tag `", body)
        self.assertIn("- zizmor: could not check ` no @all artifact `", body)
        self.assertIn("- Outside review coverage: ` @all `", body)
        self.assertIn("- Outside review coverage: ` [ok](https://evil.example) `", body)
        # The trusted reviewer identity must never post a mention or link for an attacker.
        self.assertNotIn("@all", outside_code_spans(body))
        self.assertNotIn("evil.example", outside_code_spans(body))


class ChildEnvTests(unittest.TestCase):
    KEYS = {"CURSOR_API_KEY": "c", "ANTHROPIC_API_KEY": "a", "CODEX_API_KEY": "x"}

    def test_provider_keys_are_dropped_and_the_input_is_untouched(self):
        env = {**self.KEYS, "PATH": "/bin", "HOME": "/home/reviewer", "GH_TOKEN": "forge"}
        self.assertEqual(source.child_env(env), {"PATH": "/bin", "HOME": "/home/reviewer", "GH_TOKEN": "forge"})
        self.assertEqual(env, {**self.KEYS, "PATH": "/bin", "HOME": "/home/reviewer", "GH_TOKEN": "forge"})

    def test_run_never_hands_provider_keys_to_children(self):
        with patch.dict(os.environ, self.KEYS), patch.object(source.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["git"], 0, stdout=b"ok")
            self.assertEqual(source.run(["git", "status"]), b"ok")
            for key in self.KEYS:
                self.assertNotIn(key, run.call_args.kwargs["env"])
            self.assertIn("PATH", run.call_args.kwargs["env"])
            run.reset_mock()
            source.run(["git", "status"], env={**self.KEYS, "PATH": "/bin"})
            self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/bin"})
