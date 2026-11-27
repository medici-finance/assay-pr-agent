import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import cli

# CI streams this script next to the tests, as it does k8s/.
SCRIPT = Path(__file__).resolve().parent.parent / "ci" / "capture-prompt.py"


class CapturePromptTest(unittest.TestCase):
    @property
    def clean(self):
        # The environment without the developer's own: no review settings, no Git variables (which
        # name another repository when the tests run from a Git hook), and no user or system Git
        # configuration (hooks, templates, signing, diff settings).
        env = {key: value for key, value in os.environ.items() if not key.startswith(("REVIEW_", "GIT_"))}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return env

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
            shim.write_text(f"#!/bin/sh\necho {name} >> {shlex.quote(str(self.started))}\nexit 1\n")
            shim.chmod(0o700)
        # Where the script makes its output directory, so that the tests see all it leaves behind.
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.env = dict(self.clean, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], CURSOR_API_KEY="fixture",
                        REVIEW_STATE_DIR=str(self.root / "state"), TMPDIR=str(self.tmp))

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                                        "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args], env=self.clean,
                                       cwd=self.repo, text=True).strip()

    def capture(self, *args):
        base = () if "--base" in args else ("--base", self.base)
        return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo),
                               *base, "--cache-dir", str(self.root / "cache"), *args],
                              env=self.env, capture_output=True, text=True, timeout=120)

    def written(self, result, name="prompt"):
        """The file the script said it wrote."""
        (line,) = [line for line in result.stdout.splitlines() if line.startswith(name + ": ")]
        return Path(line.split(": ", 1)[1])

    def left(self):
        """The output directories there are, each with the names in it."""
        return sorted((sorted(path.name for path in directory.iterdir()) for directory in self.tmp.iterdir()))

    def script(self, name="capture_prompt"):
        """The script as a module, for a run in this process, with its output under the test's directory."""
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        patcher = patch.object(tempfile, "tempdir", str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)
        return module

    def test_writes_the_prompt_without_starting_a_model_cli(self):
        result = self.capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.written(result).read_text()
        self.assertEqual(self.written(result).name, "prompt.txt")
        # A directory of this run's own that nobody else can open, holding the two files and nothing else.
        self.assertEqual(self.written(result).parent.parent, self.tmp)
        self.assertEqual(self.written(result).parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.left(), [["anchors.json", "prompt.txt"]])
        self.assertIn("You are PR-Reviewer", prompt)
        self.assertIn("## File: 'example.py'", prompt)
        self.assertIn("sum(row['amount'] for row in rows)", prompt)
        anchors = json.loads(self.written(result, "anchors").read_text())
        self.assertEqual(anchors, {"example.py": [1, 2]})
        self.assertIn("estimated input tokens:", result.stdout)
        self.assertIn("files left out: none", result.stdout)
        self.assertFalse(self.started.exists(), self.started.read_text() if self.started.exists() else "")

    def test_fails_when_the_review_stops_before_the_model(self):
        result = self.capture("--max-input-tokens", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("never reached the model step (state: over-budget", result.stderr)
        self.assertEqual(self.left(), [])
        self.assertFalse(self.started.exists())

    def test_a_run_that_fails_leaves_no_directory_and_each_that_succeeds_has_its_own(self):
        # However it fails: over budget, an option this script refuses, one the parser refuses, an output path.
        for options, code in ((("--max-input-tokens", "1"), 1), (("--tier", "mid"), 1), (("--estimate",), 1),
                              (("--no-such-option",), 1), (("--max-input-tokens", "many"), 2)):
            with self.subTest(options=options):
                self.assertEqual(self.capture(*options).returncode, code)
                self.assertEqual(self.left(), [])
        given = subprocess.run([sys.executable, str(SCRIPT), str(self.root / "out" / "branch"), "--repo", str(self.repo),
                                "--base", self.base], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(given.returncode, 1)
        self.assertIn("capture-prompt: usage:", given.stderr)
        self.assertIn("takes no output path", given.stderr)
        self.assertEqual((self.left(), (self.root / "out").exists()), ([], False))
        first, second = self.capture(), self.capture()
        self.assertNotEqual(self.written(first).parent, self.written(second).parent)
        self.assertEqual(self.left(), [["anchors.json", "prompt.txt"]] * 2)

    def test_reports_why_the_model_step_failed(self):
        module = self.script("capture_prompt")
        err, out = io.StringIO(), io.StringIO()
        def failed(row, *_, **__):
            # What run_pass records when the stand-in's signature no longer matches the call.
            row.update(state="failed", model_invocations=0)
            return None, "review() got an unexpected keyword argument"

        with patch.object(cli, "run_pass", failed), \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(out), patch.dict(os.environ, self.env, clear=True):
            code = module.main(["capture-prompt.py", "--repo", str(self.repo),
                                "--base", self.base, "--cache-dir", str(self.root / "cache")])
        self.assertEqual(code, 1)
        self.assertIn("state: model-unavailable", err.getvalue())
        self.assertIn("error: review() got an unexpected keyword argument", err.getvalue())

    def test_shows_what_cursor_review_printed_when_there_is_no_report(self):
        module = self.script("capture_prompt")
        err = io.StringIO()
        with patch.object(cli, "main", lambda: print("unexpected diagnostic") or 2), contextlib.redirect_stderr(err):
            code = module.main(["capture-prompt.py", "--base", self.base])
        self.assertEqual(code, 1)
        self.assertIn("unexpected diagnostic", err.getvalue())
        self.assertIn("stopped before producing a report (exit 2)", err.getvalue())

    def test_a_capture_that_cannot_be_written_leaves_neither_file(self):
        # In this process, so that the write of the prompt can be made to fail after the anchors are written.
        script = self.script("capture_prompt")
        write = Path.write_text

        def full(path, *args, **kwargs):
            if path.name == "prompt.txt":
                write(path, "half a prompt", encoding="utf-8")
                raise OSError(28, "No space left on device")
            return write(path, *args, **kwargs)

        err = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), patch.object(Path, "write_text", full), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = script.main(["capture-prompt.py", "--repo", str(self.repo), "--base", self.base,
                                "--cache-dir", str(self.root / "cache")])
        self.assertEqual(code, 1)
        self.assertIn("capture-prompt: could not write the capture", err.getvalue())
        self.assertEqual(self.left(), [])
        self.assertFalse(self.started.exists())

    def elsewhere(self):
        # Another repository, and the variables that point Git at it, with signing on that cannot work.
        other = self.root / "other"
        other.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(other)], check=True, env=self.clean)
        return other, dict(GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other), GIT_INDEX_FILE=str(other / ".git" / "index"),
                           GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="commit.gpgsign", GIT_CONFIG_VALUE_0="true",
                           GIT_CONFIG_KEY_1="gpg.program", GIT_CONFIG_VALUE_1="false")

    def test_fixture_ignores_the_developers_git_environment(self):
        # As when the suite runs from a hook of another repository.
        other, theirs = self.elsewhere()
        with patch.dict(os.environ, theirs):
            (self.repo / "more.py").write_text("x = 1\n")
            self.git("add", "more.py")
            self.git("commit", "-qm", "Committed in the fixture, unsigned")
            self.assertEqual(self.git("log", "-1", "--format=%s"), "Committed in the fixture, unsigned")
        log = subprocess.run(["git", "-C", str(other), "log", "--oneline"], capture_output=True, text=True, env=self.clean)
        self.assertEqual(log.stdout, "")  # Nothing was committed to the other repository.

    def test_capture_describes_its_repository_when_git_is_pointed_elsewhere(self):
        # The script itself is started with those variables set, as from a hook.
        other, theirs = self.elsewhere()
        head = self.git("rev-parse", "HEAD")
        self.env = dict(self.env, **{name: value for name, value in theirs.items() if not name.startswith("GIT_CONFIG")})
        result = self.capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"captured: {self.base}..{head[:12]}", result.stdout)
        self.assertNotIn("warning", result.stderr)
        self.assertIn("sum(row['amount'] for row in rows)", self.written(result).read_text())

    def test_no_provider_starts_its_cli_or_needs_a_credential(self):
        # With a key, without one, and with a home directory that holds no login of any kind.
        home = self.root / "home"
        home.mkdir()
        bare = {name: value for name, value in self.env.items()
                if not name.endswith(("_API_KEY", "_TOKEN")) and "AUTH" not in name}
        bare.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
        for provider, key in (("cursor", "CURSOR_API_KEY"), ("codex", "CODEX_API_KEY"), ("claude", "ANTHROPIC_API_KEY")):
            for name, env in (("key", dict(self.env, **{key: "fixture"})), ("no credential", bare)):
                with self.subTest(provider=provider, credential=name):
                    self.env = dict(env, REVIEW_PROVIDER=provider)
                    result = self.capture()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"provider: {provider} (not called)", result.stdout)  # The one asked for ran.
                    self.assertFalse(self.started.exists(), self.started.read_text() if self.started.exists() else "")

    def test_only_a_local_change_against_a_base_is_captured(self):
        common = [sys.executable, str(SCRIPT), "--repo", str(self.repo), "--cache-dir", str(self.root / "cache")]
        for options, said in ((["--base", ""], "--base REV is required and must not be empty"),
                              (["--base", " "], "--base REV is required and must not be empty"),
                              (["--base", self.base, "--post"], "--post is not supported"),
                              (["--base", self.base, "--model", "x"], "--model is not supported"),
                              (["--base", self.base, "--tier=mid"], "--tier is not supported"),
                              (["--change", "acme/api#3"], "--change is not supported"),
                              (["--ci"], "--ci is not supported")):
            with self.subTest(options=options):
                result = subprocess.run([*common, *options], env=self.env, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("capture-prompt: " + said, result.stderr)
                self.assertEqual(self.left(), [])
        missing = subprocess.run(common, env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(missing.returncode, 2)  # The parser's own refusal: one of its sources is required.
        self.assertFalse(self.started.exists())

    def test_says_which_commit_was_captured_and_warns_about_work_not_committed(self):
        head = self.git("rev-parse", "HEAD")
        result = self.capture()
        self.assertIn(f"captured: {self.base}..{head[:12]}", result.stdout)
        self.assertNotIn("warning", result.stderr)
        (self.repo / "example.py").write_text("def total(rows):\n    return 1\n")
        (self.repo / "new.py").write_text("y = 2\n")
        result = self.capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("capture-prompt: warning: 2 uncommitted or untracked path(s)", result.stderr)
        prompt = self.written(result).read_text()
        self.assertNotIn("return 1", prompt)  # Only what is committed is in the prompt.
        self.assertNotIn("new.py", prompt)

    def test_output_directory_inside_the_checkout_is_not_counted_as_work_not_committed(self):
        self.env = dict(self.env, TMPDIR=str(self.repo))  # As when a harness keeps its scratch space in the checkout.
        result = self.capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.written(result).parent.parent, self.repo)
        self.assertNotIn("warning", result.stderr)

    def test_stand_in_clis_record_a_start_whatever_the_path_looks_like(self):
        self.assertEqual(subprocess.run(["codex"], env=self.env).returncode, 1)
        self.assertEqual(self.started.read_text(), "codex\n")

    def test_another_commit_than_the_checkouts_is_named_and_not_warned_about(self):
        first = self.git("rev-parse", "HEAD")
        (self.repo / "example.py").write_text("def total(rows):\n    return 2\n")
        self.git("commit", "-qam", "Later work")
        (self.repo / "untracked.py").write_text("z = 3\n")
        result = self.capture("--head", first)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"captured: {self.base}..{first[:12]}", result.stdout)
        self.assertNotIn("warning", result.stderr)  # The checkout's state says nothing about that commit.
        self.assertNotIn("return 2", self.written(result).read_text())

    def test_says_so_when_the_checkout_cannot_be_checked(self):
        script = self.script("capture_prompt_unchecked")
        git = script.git
        err, out = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stderr(err), contextlib.redirect_stdout(out), \
                patch.object(script, "git", lambda repo, *args: None if args[0] == "status" else git(repo, *args)):
            code = script.main(["capture-prompt.py", "--repo", str(self.repo),
                                "--base", self.base, "--cache-dir", str(self.root / "cache")])
        self.assertEqual(code, 0)
        self.assertIn("warning: could not check", err.getvalue())

    def test_what_cursor_review_printed_is_kept_when_it_exits(self):
        result = self.capture("--base", "no-such-revision")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("capture-prompt:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.left(), [])

    def test_an_interrupted_write_leaves_nothing(self):
        script = self.script("capture_prompt_interrupted")
        write = Path.write_text

        def interrupted(path, *args, **kwargs):
            if path.name == "prompt.txt":
                raise KeyboardInterrupt
            return write(path, *args, **kwargs)

        with patch.dict(os.environ, self.env, clear=True), patch.object(Path, "write_text", interrupted), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(KeyboardInterrupt):
            script.main(["capture-prompt.py", "--repo", str(self.repo), "--base", self.base,
                         "--cache-dir", str(self.root / "cache")])
        self.assertEqual(self.left(), [])

    def test_warns_about_untracked_files_the_developer_configured_status_to_hide(self):
        self.git("config", "status.showUntrackedFiles", "no")
        (self.repo / "new.py").write_text("y = 2\n")
        result = self.capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("capture-prompt: warning: 1 uncommitted or untracked path(s)", result.stderr)

    def test_refuses_other_tiers(self):
        result = self.capture("--tier", "mid")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--tier is not supported", result.stderr)
        self.assertFalse(self.started.exists())

    def test_fails_when_the_patched_name_is_gone(self):
        module = self.script("capture_prompt")
        err = io.StringIO()
        with patch.object(cli, "review", None), contextlib.redirect_stderr(err):
            code = module.main(["capture-prompt.py", "--repo", str(self.repo),
                                "--base", self.base])
        self.assertEqual(code, 1)
        self.assertIn("cursor_pr.cli.review no longer exists", err.getvalue())
        self.assertEqual(self.left(), [])


if __name__ == "__main__":
    unittest.main()
