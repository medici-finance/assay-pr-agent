import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import cli, engine, sarif


def report(**changes):
    finding = {"path": "src/app.py", "line": 3, "end_line": 4, "title": "Unchecked input", "message": "Validate it."}
    shell = {"path": "run.sh", "rule": "SC2086", "line": 2, "message": "Double quote."}
    return {"provider": "cursor", "state": "reviewed", "findings": [finding], "checks": [
        {"tool": "shellcheck", "path": "run.sh", "state": "complete", "findings": [shell, shell | {"line": 5}]},
        {"tool": "go-analysis", "state": "complete", "findings": [
            {"path": "src/app.py", "line": 9, "rule": "SA4006", "analyzer": "staticcheck", "disposition": "new"}]}],
        "static_checks": [
            {"tool": "hadolint", "path": "Dockerfile", "source": "local", "state": "complete", "findings": [
                {"rule": "DL3007", "message": "Pin the tag.", "line": 1}]},
            {"tool": "osv-scanner", "path": "uv.lock", "source": "ci", "state": "complete", "findings": [
                {"rule": "GHSA-1234-5678-9abc", "message": "requests 2.25.1", "line": None}]}], **changes}


def location(path, line=None, end=None):
    return {"artifactLocation": {"uri": path}, "region": {"startLine": line, **({"endLine": end} if end else {})}} if line \
        else {"artifactLocation": {"uri": path}}


def results(value):
    return sarif.log(value)["runs"][0]["results"]


class SarifLogTests(unittest.TestCase):
    def test_every_finding_kind_becomes_one_result(self):
        log = sarif.log(report())
        self.assertEqual((log["$schema"], log["version"], len(log["runs"])),
                         ("https://json.schemastore.org/sarif-2.1.0.json", "2.1.0", 1))
        run = log["runs"][0]
        self.assertEqual(run["tool"]["driver"]["name"], "assay-pr-agent")
        self.assertEqual([r["id"] for r in run["tool"]["driver"]["rules"]],
                         ["model-finding", "SC2086", "SA4006", "DL3007", "GHSA-1234-5678-9abc"])
        self.assertEqual([(r["ruleId"], r["level"], r["message"]["text"], r["locations"][0]["physicalLocation"])
                          for r in run["results"]], [
            ("model-finding", "warning", "Unchecked input. Validate it.", location("src/app.py", 3, 4)),
            ("SC2086", "note", "shellcheck: Double quote.", location("run.sh", 2)),
            ("SC2086", "note", "shellcheck: Double quote.", location("run.sh", 5)),
            ("SA4006", "note", "go-analysis: staticcheck reported this as new.", location("src/app.py", 9)),
            ("DL3007", "note", "hadolint: Pin the tag.", location("Dockerfile", 1)),
            ("GHSA-1234-5678-9abc", "warning", "osv-scanner: requests 2.25.1", location("uv.lock"))])
        self.assertEqual(run["invocations"], [{"executionSuccessful": True}])

    def test_fingerprints_are_stable_for_the_same_report_and_per_line(self):
        first = results(report())
        prints = [r["partialFingerprints"]["primaryLocationLineHash"] for r in first]
        self.assertEqual(prints, [r["partialFingerprints"]["primaryLocationLineHash"] for r in results(report())])
        self.assertEqual(len(set(prints)), len(prints))
        # One rule often repeats one message in a file; the two lines stay two identities.
        self.assertNotEqual(prints[1], prints[2])
        changed = results(report(findings=[{"path": "src/app.py", "line": 3, "end_line": 4,
                                            "title": "Unchecked input", "message": "Validate it first."}]))
        self.assertNotEqual(changed[0]["partialFingerprints"], first[0]["partialFingerprints"])
        self.assertEqual([r["partialFingerprints"] for r in changed[1:]], [r["partialFingerprints"] for r in first[1:]])

    def test_messages_are_cut_and_unsafe_results_are_skipped(self):
        long = "x" * 5000
        value = report(state="partial", findings=[
            {"path": "/etc/passwd", "line": 1, "end_line": 1, "title": "Absolute", "message": "path"},
            {"path": "a/../b.py", "line": 1, "end_line": 1, "title": "Escaping", "message": "path"},
            {"path": "a\rb.py", "line": 1, "end_line": 1, "title": "Control", "message": "character"},
            {"path": "src/app.py", "line": 0, "end_line": 1, "title": "No line", "message": "location"},
            {"path": "src/app.py", "line": 3, "end_line": 3, "title": "Long", "message": long}], checks=[
            {"tool": "shellcheck", "path": "run.sh", "state": "complete", "findings": [
                {"path": "run.sh", "rule": " ", "line": 2, "message": "Blank rule"},
                {"path": "run.sh", "rule": "SC2086", "line": 2, "message": " \n"},
                {"path": "run.sh", "rule": "SC2086", "line": True, "message": "Boolean line"},
                {"path": "run.sh", "rule": "SC2086", "line": 4, "message": long}]}], static_checks=[])
        self.assertEqual([(r["ruleId"], r["message"]["text"], r["locations"][0]["physicalLocation"])
                          for r in results(value)], [
            ("model-finding", "No line. location", {"artifactLocation": {"uri": "src/app.py"}}),
            ("model-finding", "Long. " + "x" * 994, location("src/app.py", 3, 3)),
            ("SC2086", "shellcheck: Boolean line", {"artifactLocation": {"uri": "run.sh"}}),
            ("SC2086", "shellcheck: " + "x" * 988, location("run.sh", 4))])
        self.assertEqual([r["id"] for r in sarif.log(value)["runs"][0]["tool"]["driver"]["rules"]], ["model-finding", "SC2086"])
        self.assertEqual(sarif.log(value)["runs"][0]["invocations"], [{"executionSuccessful": False}])

    def test_run_records_whether_the_review_completed(self):
        for state, complete in (("reviewed", True), ("empty", True), ("estimate", True), ("partial", False),
                                ("over-budget", False), ("model-unavailable", False)):
            with self.subTest(state=state):
                self.assertIs(sarif.log(report(state=state))["runs"][0]["invocations"][0]["executionSuccessful"], complete)


class SarifFlagTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("commit", "--allow-empty", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.root / "example.sh").write_text("#!/bin/sh\necho $argument\n")
        self.git("add", "example.sh")
        self.git("commit", "-qm", "change")
        # A provider key left in the shell would make the CLI contact the account service first.
        self.start(patch.dict(os.environ, {"REVIEW_STATE_DIR": str(self.root / "state"),
                                           "REVIEW_PROVIDER": "cursor", "REVIEW_AUTH_MODE": "api-key"}))
        for key in ("REVIEW_MAX_INPUT_TOKENS", "CURSOR_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY"):
            os.environ.pop(key, None)
        finding = {"path": "example.sh", "rule": "SC2086", "line": 2, "message": "Double quote to prevent globbing."}
        self.start(patch.object(cli, "scan_files", return_value=[
            {"tool": "shellcheck", "path": "example.sh", "state": "complete", "findings": [finding]}]))

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
                                       cwd=self.root, text=True).strip()

    def check(self, *args, cwd=None):
        argv = ["cursor-review", "--repo", str(self.root), "--cache-dir", str(self.root / "cache"),
                "--max-input-tokens", "1000000", *args]
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", argv), contextlib.chdir(cwd or self.root), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main()
        return code, out.getvalue(), err.getvalue()

    def test_estimate_and_review_write_the_log_relative_to_the_caller(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        code, out, _ = self.check("--base", self.base, "--estimate", "--sarif", "estimate.sarif", cwd=elsewhere)
        self.assertEqual(code, 0)
        exported = (elsewhere / "estimate.sarif").read_text()
        self.assertEqual(exported, json.dumps(sarif.log(json.loads(out)), ensure_ascii=True, indent=2) + "\n")
        self.assertEqual(json.loads(out)["state"], "estimate")
        self.assertEqual([(r["ruleId"], r["level"]) for r in json.loads(exported)["runs"][0]["results"]], [("SC2086", "note")])
        self.assertFalse((self.root / "estimate.sarif").exists())
        answer = {"findings": [{"path": "example.sh", "line": 2, "end_line": 2, "title": "Argument splitting",
                                "message": "Whitespace splits the argument."}], "cursor_usage": None}
        with patch.object(engine, "invoke", return_value=answer):
            code, out, _ = self.check("--base", self.base, "--sarif", str(self.root / "review.sarif"))
        self.assertEqual(code, 0)
        self.assertEqual([(r["ruleId"], r["level"]) for r in json.loads((self.root / "review.sarif").read_text())["runs"][0]["results"]],
                         [("model-finding", "warning"), ("SC2086", "note")])
        self.assertEqual(json.loads(out)["findings"], answer["findings"])
        self.check("--base", self.base, "--estimate")
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.suffix == ".sarif"), ["review.sarif"])

    def test_failed_write_reports_the_reason_and_keeps_the_report(self):
        code, out, err = self.check("--base", self.base, "--estimate", "--sarif", str(self.root / "missing" / "out.sarif"))
        self.assertEqual((code, json.loads(out)["state"]), (2, "estimate"))
        self.assertIn("cursor-review:", err)
        self.assertNotIn("missing", out)
        self.assertFalse((self.root / "missing").exists())
