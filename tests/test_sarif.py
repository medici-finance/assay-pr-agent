import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import cli, engine, sarif
from cursor_pr.checks import blob
from cursor_pr.config import project_from
from cursor_pr.source import local_change


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


def results(value, source=None):
    return sarif.log(value, source)["runs"][0]["results"]


def prints(rows):
    return [row.get("partialFingerprints") for row in rows]


SCRIPT = b"#!/bin/sh\nfor f in $list; do\n  echo $f\n  cp $f $dir\ndone\n"
SOURCES = {"run.sh": SCRIPT, "src/app.py": b"import os\n" * 12, "copy.py": b"import os\n" * 12, "Dockerfile": b"FROM node:latest\n"}
EXAMPLE = "#!/bin/sh\necho $argument\n"
SHELL = {"path": "example.sh", "rule": "SC2086", "line": 2, "message": "Double quote to prevent globbing."}


def head_text(path):
    return SOURCES.get(path)


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

    def test_line_hashes_are_what_the_upload_action_computes(self):
        # The values come from github/codeql-action's src/fingerprints.ts, run on each input with Node.
        line = b"alpha beta gamma delta epsilon zeta eta theta iota kappa\n"
        for content, expected in [
                (b"alpha\nbeta\n gamma ", {1: "caf354a73f9d40e4:1", 2: "fbbdc98f9741703c:1", 3: "1b3f2700d8425376:1"}),
                (b"one\r\ntwo\r\nthree\nfour\r\n\nfive\r\nlast", {
                    1: "ac35a45d0e35f31:1", 2: "2db9c0e4851414ad:1", 3: "487f480864c8efc5:1", 4: "d97408ce4b120c77:1",
                    5: "2323a9ec123ef599:1", 6: "1c076918a3f8ecb3:1", 7: "58429ecce5a8d0db:1"}),
                ("\ufeff bom first\ncafé naïve\r\n日本語のテキスト\nemoji 😀 here\nmixed 😀 日本 café\n".encode(), {
                    1: "9a8964ff31a1d468:1", 2: "3dadecd5cc4eb1d9:1", 3: "6817eb4fc947bccf:1",
                    4: "82c8198bfd7b3c64:1", 5: "4a730712a5277991:1"}),
                # Six equal lines in the middle share a hash and differ by their number of occurrence.
                (line * 8, {**{n: f"eb864801a0651d03:{n}" for n in range(1, 7)}, 7: "eb86480265f40c45:1",
                            8: "d88794b44ad72f94:1"}),
                (b"", {})]:
            with self.subTest(content=content[:24]):
                self.assertEqual(sarif.line_hashes(content), expected)

    def test_only_requested_line_hashes_are_returned(self):
        content = b"same line\n" * 30
        all_hashes = sarif.line_hashes(content)
        self.assertEqual(sarif.line_hashes(content, {2, 7, 50}), {n: all_hashes[n] for n in (2, 7)})

    def test_located_results_are_identified_by_their_line_text(self):
        def export(*lines, text=SCRIPT):
            value = report(findings=[], static_checks=[], checks=[{
                "tool": "shellcheck", "path": "run.sh", "state": "complete", "findings": [
                    {"path": "run.sh", "rule": "SC2086", "line": line, "message": "Double quote."} for line in lines]}])
            return prints(results(value, lambda path: text))
        first = export(2, 4)
        self.assertEqual(first, export(2, 4))
        # One rule often repeats a message in a file; the two lines stay two identities.
        self.assertNotEqual(first[0], first[1])
        # A line added above moves both findings down without touching either one.
        below = b"#!/bin/sh\nset -eu\nfor f in $list; do\n  echo $f\n  cp $f $dir\ndone\n"
        self.assertEqual(first, export(3, 5, text=below))
        # Editing a finding's own line gives it a new identity and leaves a later finding alone.
        edited = export(2, 4, text=SCRIPT.replace(b"for f in", b"for file in"))
        self.assertNotEqual(edited[0], first[0])
        self.assertEqual(edited[1], first[1])
        # The 100 characters after a line are part of its value, so this edit reaches both findings.
        edited = export(2, 4, text=SCRIPT.replace(b"cp $f", b"mv $f"))
        self.assertEqual([value != old for value, old in zip(edited, first)], [True, True])

    def test_findings_of_one_rule_on_one_line_are_told_apart(self):
        def model(*findings):
            return report(findings=[{"path": "src/app.py", "line": 3, "end_line": end, "title": "T", "message": message}
                                    for message, end in findings], checks=[], static_checks=[])

        def scanned(*findings):
            return report(findings=[], static_checks=[], checks=[
                {"tool": "shellcheck", "path": "run.sh", "state": "complete", "findings": list(findings)}])
        value = sarif.line_hashes(SOURCES["src/app.py"])[3]
        # Every model finding has one rule. Distinct findings on one line keep distinct identities
        # even when the model changes their order.
        same_line = [("first", 3), ("same", 3), ("same", 5), ("third", 3), ("first", 3)]
        keys = prints(results(model(*same_line), head_text))
        self.assertEqual(keys, list(reversed(prints(results(model(*reversed(same_line)), head_text)))))
        identities = [key["primaryLocationLineHash"] for key in keys]
        self.assertTrue(all(key.startswith(value + ":") for key in identities))
        self.assertEqual(len(set(identities[:4])), 4)
        self.assertEqual(identities[0], identities[-1])
        # The finding keeps its identity when the others are removed.
        self.assertEqual(prints(results(model(("first", 3)), head_text)), keys[:1])
        # The log cannot tell apart two SC2086 rows with one message, so they keep one identity.
        shell = {"path": "run.sh", "rule": "SC2086", "line": 4, "message": "Double quote."}
        one = prints(results(scanned(shell), head_text))
        self.assertEqual(prints(results(scanned(shell, shell), head_text)), one * 2)
        # The result keeps its fingerprint regardless of a different rule on the same line.
        other = dict(shell, rule="SC2154", message="Referenced but not assigned.")
        self.assertEqual(prints(results(scanned(shell, other), head_text))[:1], one)
        # A finding in another file does not change this result.
        alone = model(("first", 3))
        self.assertEqual(prints(results(dict(alone, findings=alone["findings"] + [
            dict(alone["findings"][0], path="copy.py", message="elsewhere")]), head_text)),
            keys[:1] + prints(results(model(("elsewhere", 3)), head_text)))
        first, second = prints(results(model(("first", 3), ("second", 3))))
        self.assertNotEqual(first, second)
        self.assertEqual(prints(results(model(("second", 3), ("first", 3)))), [second, first])
        self.assertEqual(prints(results(model(("first", 3)))), [first])

    def test_unreadable_source_falls_back_to_line_numbers(self):
        values = prints(results(report()))
        self.assertEqual(values, prints(results(report(), lambda path: None)))
        # The two shellcheck results have the same rule and message on different lines.
        self.assertNotEqual(values[1], values[2])
        self.assertTrue(values[1]["primaryLocationLineHash"].startswith("line:2:"))
        self.assertTrue(values[2]["primaryLocationLineHash"].startswith("line:5:"))
        past_end = report(findings=[{"path": "src/app.py", "line": 13, "end_line": 13, "title": "T", "message": "m"}],
                          checks=[], static_checks=[])
        fingerprint = prints(results(past_end, head_text))[0]["primaryLocationLineHash"]
        self.assertTrue(fingerprint.startswith("line:13:"))

    def test_lineless_results_are_identified_by_path_rule_and_message(self):
        advisories = [("GHSA-1234-5678-9abc", "requests 2.25.1"), ("GHSA-1234-5678-9abc", "urllib3 1.26.5"),
                      ("GHSA-0000-1111-2222", "requests 2.25.1")]
        value = report(findings=[], checks=[], static_checks=[
            {"tool": "osv-scanner", "path": "uv.lock", "source": "ci", "state": "complete", "findings": [
                {"rule": rule, "message": message, "line": None} for rule, message in advisories]}])
        self.assertEqual(prints(results(value)), [{"primaryLocationLineHash": hashlib.sha256(
            json.dumps(["uv.lock", rule, "osv-scanner: " + message]).encode()).hexdigest()} for rule, message in advisories])

    def test_fingerprints_include_text_beyond_the_display_limit(self):
        prefix = "x" * sarif.MESSAGE_LIMIT
        finding = {"path": "src/app.py", "line": 3, "end_line": 3, "title": "Issue"}
        for line in (3, None):
            with self.subTest(line=line):
                value = report(findings=[dict(finding, line=line, message=prefix + suffix)
                                         for suffix in ("first defect", "second defect")],
                               checks=[], static_checks=[])
                first, second = results(value, head_text)
                self.assertEqual(first["message"], second["message"])
                self.assertNotEqual(first["partialFingerprints"], second["partialFingerprints"])

    def test_paths_are_percent_encoded_uri_references(self):
        paths = ["src/a#b.py", "sp ace/ü.py", "50%.yml", "q?x.yml", "plain/dir/file.sh"]
        asked = []
        value = report(findings=[{"path": path, "line": 1, "end_line": 1, "title": "T", "message": "m"} for path in paths],
                       checks=[], static_checks=[])
        rows = results(value, lambda path: asked.append(path) or b"first\n")
        self.assertEqual([r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] for r in rows],
                         ["src/a%23b.py", "sp%20ace/%C3%BC.py", "50%25.yml", "q%3Fx.yml", "plain/dir/file.sh"])
        # The source is read by its Git path, not by the URI.
        self.assertEqual(asked, paths)

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
        (self.root / "example.sh").write_text(EXAMPLE)
        self.git("add", "example.sh")
        self.git("commit", "-qm", "change")
        # A provider key left in the shell would make the CLI contact the account service first.
        self.start(patch.dict(os.environ, {"REVIEW_STATE_DIR": str(self.root / "state"),
                                           "REVIEW_PROVIDER": "cursor", "REVIEW_AUTH_MODE": "api-key"}))
        for key in ("REVIEW_MAX_INPUT_TOKENS", "CURSOR_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY"):
            os.environ.pop(key, None)
        self.start(patch.object(cli, "scan_files", return_value=[
            {"tool": "shellcheck", "path": "example.sh", "state": "complete", "findings": [dict(SHELL)]}]))

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
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(exported, json.dumps(sarif.log(json.loads(out), lambda path: blob(self.root, head, path)),
                                              ensure_ascii=True, indent=2) + "\n")
        # The fingerprint is of the finding's line as committed.
        key = json.loads(exported)["runs"][0]["results"][0]["partialFingerprints"]["primaryLocationLineHash"]
        self.assertTrue(key.startswith(sarif.line_hashes(EXAMPLE.encode())[2] + ":"))
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

    def test_export_reuses_the_temporary_managed_checkout(self):
        candidate = self.root
        base = self.git("rev-parse", "HEAD")
        path = "example.py"
        (candidate / path).write_text("value = 1\n")
        self.git("add", path)
        self.git("commit", "-qm", "Add a Python change")
        original = self.root / "checkout-without-head"
        original.mkdir()
        url = "https://github.com/example/repo/pull/1"
        project = project_from(dict(provider="github", url=url.removesuffix("/pull/1"), requester_ids=[7],
                                    checkout=str(original), managed=True))
        change = local_change(candidate, base, "HEAD")
        change = type(change)(change.head, change.files, url)
        active = False

        @contextlib.contextmanager
        def checkout(*_):
            nonlocal active
            active = True
            try:
                yield candidate
            finally:
                active = False

        def read(root, head, path):
            self.assertTrue(active)
            self.assertEqual(root, candidate)
            return blob(root, head, path)

        target = candidate / "managed.sarif"
        self.root = original
        answer = {"findings": [{"path": path, "line": 1, "end_line": 1, "title": "Example",
                                "message": "A finding on a path without a scanner."}], "cursor_usage": None}
        with patch.object(cli, "scan_checkout", side_effect=checkout), patch.object(cli, "blob", side_effect=read), \
             patch.object(cli, "forge_change", return_value=change), \
             patch.object(cli, "scan_files", return_value=[]), patch.object(engine, "invoke", return_value=answer), \
             patch.dict(os.environ, {"CURSOR_REVIEW_POLLER_CHILD": "1", "REVIEW_FORGE_PROJECT": json.dumps(dict(
                 provider=project.provider, url=project.url, checkout=str(project.checkout),
                 requester_ids=sorted(project.requesters), managed=True))}):
            code, _, error = self.check("--change", "example/repo#1", "--sarif", str(target))
        self.assertEqual(code, 0, error)
        row, = json.loads(target.read_text())["runs"][0]["results"]
        self.assertIn("partialFingerprints", row)
        self.assertFalse(active)

    def test_fingerprints_read_the_reviewed_head(self):
        # Edited after the commit, and absent from it: neither line is at the reviewed head.
        (self.root / "example.sh").write_text("#!/bin/sh\nprintf '%s\\n' \"$argument\"\n")
        self.start(patch.object(cli, "scan_files", return_value=[
            {"tool": "shellcheck", "path": path, "state": "complete", "findings": [dict(SHELL, path=path)]}
            for path in ("example.sh", "ghost.sh")]))
        code, _, _ = self.check("--base", self.base, "--estimate", "--sarif", str(self.root / "out.sarif"))
        self.assertEqual(code, 0)
        rows = json.loads((self.root / "out.sarif").read_text())["runs"][0]["results"]
        self.assertEqual([r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] for r in rows], ["example.sh", "ghost.sh"])
        self.assertTrue(rows[0]["partialFingerprints"]["primaryLocationLineHash"].startswith(
            sarif.line_hashes(EXAMPLE.encode())[2] + ":"))
        self.assertTrue(rows[1]["partialFingerprints"]["primaryLocationLineHash"].startswith("line:2:"))
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(blob(self.root, head, "example.sh"), EXAMPLE.encode())
        self.assertIsNone(blob(self.root, head, "ghost.sh"))
        self.assertIsNone(blob(self.root, head, "example.sh", limit=len(EXAMPLE) - 1))

    def test_failed_write_reports_the_reason_and_keeps_the_report(self):
        code, out, err = self.check("--base", self.base, "--estimate", "--sarif", str(self.root / "missing" / "out.sarif"))
        self.assertEqual((code, json.loads(out)["state"]), (2, "estimate"))
        self.assertIn("cursor-review:", err)
        self.assertNotIn("missing", out)
        self.assertFalse((self.root / "missing").exists())

    def test_symlink_loop_does_not_prevent_printing_the_report(self):
        loop = self.root / "loop.sarif"
        loop.symlink_to(loop.name)
        code, out, err = self.check("--base", self.base, "--estimate", "--sarif", str(loop))
        self.assertEqual((code, json.loads(out)["state"]), (2, "estimate"))
        self.assertIn("cursor-review:", err)
        self.assertTrue(loop.is_symlink())

    def test_existing_output_link_is_rejected(self):
        target = self.root / "original.txt"
        target.write_text("keep me")
        link = self.root / "review.sarif"
        link.symlink_to(target.name)
        code, out, err = self.check("--base", self.base, "--estimate", "--sarif", str(link))
        self.assertEqual((code, json.loads(out)["state"]), (2, "estimate"))
        self.assertIn("symbolic link", err)
        self.assertEqual(target.read_text(), "keep me")
        self.assertTrue(link.is_symlink())

    def test_replacing_a_private_report_keeps_it_private(self):
        target = self.root / "review.sarif"
        target.write_text("old report")
        target.chmod(0o600)
        previous = os.umask(0o022)
        try:
            code, _, error = self.check("--base", self.base, "--estimate", "--sarif", str(target))
        finally:
            os.umask(previous)
        self.assertEqual(code, 0, error)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(target.read_text())["version"], "2.1.0")
