import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import cli

# CI streams this script next to the tests, as it does k8s/.
SCRIPT = Path(__file__).resolve().parent.parent / "ci" / "capture-prompt.py"


class CapturePromptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "example.py").write_text("def total(rows):\n    return 0\n")
        self.git("add", "example.py")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.repo / "example.py").write_text("def total(rows):\n    return sum(row['amount'] for row in rows)\n")
        self.git("commit", "-qam", "Sum the amounts")
        # Every provider CLI on PATH records that it was started.
        self.started = self.root / "started"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name in ("cursor-agent", "codex", "claude"):
            shim = bin_dir / name
            shim.write_text(f"#!/bin/sh\necho {name} >> {self.started}\nexit 1\n")
            shim.chmod(0o700)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("REVIEW_")}
        self.env.update(PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], CURSOR_API_KEY="fixture",
                        REVIEW_STATE_DIR=str(self.root / "state"))

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
                                       cwd=self.repo, text=True).strip()

    def capture(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.root / "out" / "branch"), "--repo", str(self.repo),
                               "--base", self.base, "--cache-dir", str(self.root / "cache"), *args],
                              env=self.env, capture_output=True, text=True, timeout=120)

    def test_writes_the_prompt_without_starting_a_model_cli(self):
        result = self.capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = (self.root / "out" / "branch.prompt.txt").read_text()
        self.assertIn("You are PR-Reviewer", prompt)
        self.assertIn("## File: 'example.py'", prompt)
        self.assertIn("sum(row['amount'] for row in rows)", prompt)
        anchors = json.loads((self.root / "out" / "branch.anchors.json").read_text())
        self.assertEqual(anchors, {"example.py": [1, 2]})
        self.assertIn("estimated input tokens:", result.stdout)
        self.assertIn("files left out: none", result.stdout)
        self.assertFalse(self.started.exists(), self.started.read_text() if self.started.exists() else "")

    def test_fails_when_the_review_stops_before_the_model(self):
        result = self.capture("--max-input-tokens", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("never reached the model step (state: over-budget", result.stderr)
        self.assertFalse((self.root / "out" / "branch.prompt.txt").exists())
        self.assertFalse(self.started.exists())

    def test_a_failed_run_removes_an_earlier_prompt(self):
        self.assertEqual(self.capture().returncode, 0)
        result = self.capture("--max-input-tokens", "1")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / "out" / "branch.prompt.txt").exists())
        self.assertFalse((self.root / "out" / "branch.anchors.json").exists())

    def test_reports_why_the_model_step_failed(self):
        spec = importlib.util.spec_from_file_location("capture_prompt", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        err, out = io.StringIO(), io.StringIO()
        def failed(row, *_, **__):
            # What run_pass records when the stand-in's signature no longer matches the call.
            row.update(state="failed", model_invocations=0)
            return None, "review() got an unexpected keyword argument"

        with patch.object(cli, "run_pass", failed), \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(out), patch.dict(os.environ, self.env, clear=True):
            code = module.main(["capture-prompt.py", str(self.root / "out" / "branch"), "--repo", str(self.repo),
                                "--base", self.base, "--cache-dir", str(self.root / "cache")])
        self.assertEqual(code, 1)
        self.assertIn("state: model-unavailable", err.getvalue())
        self.assertIn("error: review() got an unexpected keyword argument", err.getvalue())

    def test_refuses_other_tiers(self):
        result = self.capture("--tier", "mid")
        self.assertEqual(result.returncode, 1)
        self.assertIn("only the regular tier", result.stderr)
        self.assertFalse(self.started.exists())

    def test_fails_when_the_patched_name_is_gone(self):
        spec = importlib.util.spec_from_file_location("capture_prompt", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        err = io.StringIO()
        with patch.object(cli, "review", None), contextlib.redirect_stderr(err):
            code = module.main(["capture-prompt.py", str(self.root / "out" / "branch"), "--repo", str(self.repo),
                                "--base", self.base])
        self.assertEqual(code, 1)
        self.assertIn("cursor_pr.cli.review no longer exists", err.getvalue())
        self.assertFalse((self.root / "out" / "branch.prompt.txt").exists())


if __name__ == "__main__":
    unittest.main()
