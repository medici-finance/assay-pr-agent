import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import checks, cli, engine, source


class ReviewTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("commit", "--allow-empty", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.root/"example.sh").write_text('#!/bin/sh\necho $argument\n')
        self.git("add", "example.sh")
        self.git("commit", "-qm", "change")
        self.head = self.git("rev-parse", "HEAD")
        self.calls = self.root/"calls"
        self.shim = self.root/"cursor-agent"
        issues = [{"relevant_file":"example.sh","start_line":2,"end_line":2,
                   "issue_header":"Argument splitting","issue_content":"Whitespace splits the argument."}]
        envelope = {"type":"result","subtype":"success","is_error":False,
                    "result":json.dumps({"review":{"key_issues_to_review":issues}})}
        self.shim.write_text("#!/usr/bin/env python3\nimport json,os,pathlib,sys\n"
            "assert sys.argv[1:5]==['-p','--mode=ask','--output-format','json']\n"
            "assert json.loads(pathlib.Path('.cursor/cli.json').read_text())['permissions']['allow']==[]\n"
            "prompt = sys.stdin.read()\n"
            "assert sys.argv[5:] in ([], ['--trust'], ['--trust', '--model', sys.argv[-1]])\n"
            "assert 'SC2086' in prompt and 'example.sh' in prompt\n"
            "assert 'GH_TOKEN' not in os.environ and not pathlib.Path('.git').exists()\n"
            f"with open({str(self.calls)!r}, 'a') as f: f.write('call\\n')\n"
            f"print({json.dumps(envelope)!r})\n")
        self.shim.chmod(0o700)
        env = patch.dict(os.environ, {"PATH":str(self.root)+os.pathsep+os.environ["PATH"],"GH_TOKEN":"fixture",
                                      "REVIEW_STATE_DIR":str(self.root/"state")})
        env.start()
        self.addCleanup(env.stop)
        # Tests exercise the saved and default budgets; a shell REVIEW_MAX_INPUT_TOKENS would win over both.
        for name in ("REVIEW_MAX_INPUT_TOKENS", *cli.FINDINGS_SETTINGS.values()):
            os.environ.pop(name, None)

    def git(self, *args):
        return subprocess.check_output(["git","-c","user.name=Test","-c","user.email=test@example.org",
                                        *args],cwd=self.root,text=True).strip()

    def check(self,*args):
        argv = ["cursor-review","--repo",str(self.root),"--cache-dir",str(self.root/"cache"),*args]
        out = io.StringIO()
        with patch("sys.argv",argv),contextlib.redirect_stdout(out):
            code = cli.main()
        return code,json.loads(out.getvalue())

    def test_budget_and_reuse(self):
        args = ["--base",self.base]
        self.assertEqual(self.check(*args,"--estimate")[1]["state"],"estimate")
        self.assertEqual(self.check(*args,"--max-input-tokens","1")[1]["state"],"over-budget")
        self.assertFalse(self.calls.exists())
        code,result = self.check(*args)
        self.assertEqual(code,0,result)
        self.assertEqual(result["findings"][0]["line"],2)
        self.assertTrue(self.check(*args)[1]["cache_hit"])
        self.assertEqual(self.calls.read_text().splitlines(),["call"])
        # The messages are part of the prompt: a new head with the same patch and message
        # reuses the result, and a different message would be a different review.
        self.git("commit", "--amend", "--no-edit", "-q", "--date", "2001-02-03T04:05:06Z")
        self.assertTrue(self.check(*args)[1]["cache_hit"])
        self.assertFalse(self.check(*args, "--model", "another-model")[1]["cache_hit"])
        self.assertEqual(self.calls.read_text().splitlines(), ["call", "call"])

    def test_scanner_marks_an_equivalent_shifted_warning_without_hiding_it(self):
        script = self.root / "example.sh"
        tail = "# Padding outside the changed hunk.\n" * 20 + "echo $later\n"
        old_source = "#!/bin/sh\necho $argument\n" + tail
        script.write_text(old_source)
        self.git("add", "example.sh")
        self.git("commit", "-qm", "Add a distant scanner warning")
        old_head = self.git("rev-parse", "HEAD")
        script.write_text("#!/bin/sh\n# The existing warning moves.\necho $argument\necho $new\n" + tail)
        self.git("add", "example.sh")
        self.git("commit", "-qm", "Add another argument")
        warning = {"code": 2086, "column": 6, "endColumn": 8, "level": "info", "message": "Quote the argument."}
        shifted = dict(warning, line=3, endLine=3)
        new = dict(warning, line=4, endLine=4)
        previous = dict(warning, line=2, endLine=2)
        distant = dict(warning, line=25, endLine=25)
        distant_previous = dict(warning, line=23, endLine=23)
        for old, expected in (([previous], [3]), ([dict(previous, level="style")], [])):
            with self.subTest(previous=old):
                with patch.object(checks.shutil, "which", return_value="/scanner"), \
                     patch.object(checks, "scan_blob", side_effect=[[shifted, new, distant], [*old, distant_previous]]) as scan, \
                     patch.object(engine, "invoke", return_value={"findings": [], "cursor_usage": None}):
                    code, report = self.check("--base", old_head)
                check, = report["checks"]
                self.assertEqual(code, 0)
                self.assertEqual([row["line"] for row in check["findings"]], [3, 4, 25])
                self.assertEqual([row["line"] for row in check["findings"] if row.get("baseline_match")], [*expected, 25])
                self.assertEqual([row["line"] for row in check["findings"] if row.get("shown_to_model")], expected)
                self.assertEqual(check["existing_findings"], 1 + len(expected))
                self.assertEqual([call.args[2] for call in scan.call_args_list],
                                 [script.read_bytes(), old_source.encode()])
                if expected:
                    change = source.local_change(self.root, old_head, "HEAD")
                    self.assertIn('"baseline_match": true', engine.pack(change.files, [check])[0])
                    with patch.object(cli, "post_comment") as post:
                        cli.publish("example/repo#1", change.head, report)
                    body = post.call_args.args[3]
                    self.assertIn("Scanner diagnostics also reported on the base: 1.", body)
                    self.assertIn(":4 ", body)
                    self.assertIn(":25 ", body)
                    self.assertNotIn(":3 ", body)
                    with patch.object(cli, "post_comment") as post:
                        cli.publish("example/repo#1", change.head, dict(report, state="degraded"))
                    self.assertIn(":3 ", post.call_args.args[3])
        with patch.object(checks.shutil, "which", return_value="/scanner"), \
             patch.object(checks, "scan_blob", side_effect=[[shifted, new], source.ReviewError("base unavailable")]):
            code, report = self.check("--base", old_head, "--estimate")
        self.assertEqual(code, 0)
        self.assertEqual([row["line"] for row in report["checks"][0]["findings"]], [3, 4])
        self.assertNotIn("existing_findings", report["checks"][0])

    def test_baseline_budget_excludes_time_spent_scanning_head(self):
        diff = "@@ -1 +1,2 @@\n+#!/bin/sh\n echo $value\n"
        files = [source.File(path, diff, "modified") for path in ("first.sh", "second.sh")]
        old, head = b"echo $value\n", b"#!/bin/sh\necho $value\n"
        elapsed = 0
        calls = []

        def scan(tool, args, content, timeout=20):
            nonlocal elapsed
            calls.append(content)
            elapsed += 16 if content == head else 3
            return [{"line": 2 if content == head else 1, "code": 2086, "message": "Quote the argument."}]

        with patch.object(checks.time, "monotonic", side_effect=lambda: elapsed), \
             patch.object(checks.shutil, "which", return_value="/scanner"), \
             patch.object(checks, "blob", side_effect=[head, old, head, old]), \
             patch.object(checks, "scan_blob", side_effect=scan):
            results = checks.scan_files(self.root, "head", files, base="base")
        self.assertEqual(calls, [head, old, head, old])
        self.assertEqual([check["existing_findings"] for check in results], [1, 1])

    def test_baseline_stops_preparing_candidates_at_the_deadline(self):
        file = source.File("script.sh", "@@ -1 +1,2 @@\n+#!/bin/sh\n echo $value\n", "modified")
        warning = {"line": 2, "code": 2086, "message": "Quote the argument."}
        elapsed = 0

        def map_line(*args):
            nonlocal elapsed
            elapsed += 2
            return 1

        with patch.object(checks.time, "monotonic", side_effect=lambda: elapsed), \
             patch.object(checks, "base_line", side_effect=map_line) as mapping, \
             patch.object(checks, "scan_blob") as scan:
            matches = checks.baseline_matches(file, [warning] * 10, b"#!/bin/sh\necho $value\n",
                                              b"echo $value\n", "shellcheck", [], 3)
        self.assertFalse(matches)
        self.assertEqual(mapping.call_count, 2)
        scan.assert_not_called()

    def test_baseline_git_reads_share_the_remaining_budget(self):
        with patch.object(checks.time, "monotonic", side_effect=[0, 3]), \
             patch.object(checks, "git", side_effect=[b"4", b"text"]) as git:
            self.assertEqual(checks.blob(self.root, "base", "file.sh", deadline=5), b"text")
        self.assertEqual([call.kwargs["timeout"] for call in git.call_args_list], [5, 2])

    def test_actionlint_baseline_maps_related_locations(self):
        old = b"jobs:\n  build: {}\n  build: {}\n"
        head = b"# Shift the whole file\n" + old
        file = source.File(".github/workflows/ci.yml", "@@ -1,3 +1,4 @@\n+# Shift the whole file\n"
                           " jobs:\n   build: {}\n   build: {}\n", "modified")
        existing = {"line": 3, "column": 3, "kind": "syntax", "message": "previously defined at line:2,col:3"}
        shifted = dict(existing, line=4, message="previously defined at line:3,col:3")
        with patch.object(checks, "scan_blob", return_value=[existing]):
            self.assertEqual(checks.baseline_matches(file, [shifted], head, old, "actionlint", [], float("inf")), {0})

    def test_budget_comes_from_saved_setup_unless_overridden(self):
        bundle = self.root/"state/.review-setup-fixture"
        bundle.mkdir(parents=True)
        settings = bundle/"settings.json"
        settings.write_text(json.dumps({"settings": {"REVIEW_MAX_INPUT_TOKENS": "1"}, "projects": [], "secrets": {}}))
        (self.root/"state/setup").symlink_to(bundle.name)
        args = ["--base", self.base]
        self.assertEqual(self.check(*args)[1]["state"], "over-budget")
        self.assertEqual(self.check(*args, "--max-input-tokens", "1000000", "--estimate")[1]["state"], "estimate")
        with patch.dict(os.environ, {"REVIEW_MAX_INPUT_TOKENS": "1000000"}):
            self.assertEqual(self.check(*args, "--estimate")[1]["state"], "estimate")
        settings.write_text(json.dumps({"settings": {}, "projects": [], "secrets": {}}))
        self.assertEqual(self.check(*args, "--estimate")[1]["max_input_tokens"], 6000)
        settings.write_text(json.dumps({"settings": {"REVIEW_MAX_INPUT_TOKENS": "bad"}, "projects": [], "secrets": {}}))
        out = io.StringIO()
        with patch("sys.argv", ["cursor-review", "--repo", str(self.root), "--base", self.base]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(cli.main(), 2)
        self.assertIn("REVIEW_MAX_INPUT_TOKENS must be a positive integer", out.getvalue())

    def test_go_coverage_and_gate(self):
        report = self.root/"go.json"
        row = dict(path="example.go", line=4, rule="unused", analyzer="lint", disposition="new")
        value = dict(schema="go-analysis/v1", source_sha=self.head, state="complete", errors=[],
                     coverage=[{"state": "complete"}], expected_scans=1, findings=[row],
                     totals=dict(new=1, worsened=0, existing=0, resolved=0), gate="fail", exit_code=1)
        report.write_text(json.dumps(value))
        self.assertEqual(cli.go_report(report, self.head)["findings"], [row])
        for mismatched in [dict(expected_scans=2), dict(exit_code=0), dict(coverage=[{"state":"could-not-check"}])]:
            report.write_text(json.dumps(dict(value, **mismatched)))
            self.assertEqual(cli.go_report(report, self.head)["state"], "could-not-check")

    def test_ci_refs(self):
        event = self.root/"event"
        event.write_text(json.dumps({"pull_request":{"head":{"sha":self.head},"base":{"sha":self.base}}}))
        for env in [{"GITHUB_ACTIONS":"true","GITHUB_EVENT_NAME":"pull_request","GITHUB_EVENT_PATH":str(event)},
                    {"CI_PIPELINE_SOURCE":"merge_request_event","CI_MERGE_REQUEST_DIFF_BASE_SHA":self.base,
                     "CI_COMMIT_SHA":self.head},
                    {"CI_PIPELINE_SOURCE":"merge_request_event","CI_MERGE_REQUEST_DIFF_BASE_SHA":self.base,
                     "CI_MERGE_REQUEST_EVENT_TYPE":"merged_result","CI_COMMIT_SHA":"0"*40,
                     "CI_MERGE_REQUEST_SOURCE_BRANCH_SHA":self.head}]:
            with patch.dict(os.environ,env):
                code,report = self.check("--ci")
                self.assertEqual((code,report["head"]),(0,self.head),report)
        self.assertEqual(len(self.calls.read_text().splitlines()),1)

    def forge_fixture(self, provider):
        from cursor_pr.config import project_from
        project = project_from(dict(provider=provider, url=f"https://{provider}.com/org/repo",
                                    checkout=str(self.root), requester_ids=[1]))
        files = {"headSha": self.head, "complete": True, "changedFiles": 1, "files": [{
            "filename": "example.sh", "patchAbsent": False, "status": "added",
            "patch": "@@ -0,0 +1,2 @@\n+#!/bin/sh\n+echo $argument\n"}]}
        detail = {"url": project.change_url(1), "headSha": self.head, "state": "open",
                  "baseRef": "main", "changedFiles": 1}
        return project, [detail, files, detail]

    def test_forge_read_and_post_arguments(self):
        for provider in ("github", "gitlab"):
            project, reads = self.forge_fixture(provider)
            with patch.object(cli, "load_config", return_value={project.key: project}), \
                 patch.object(cli, "forge_credentials"), \
                 patch.object(source, "run_forge", side_effect=reads) as read, \
                 patch.object(cli, "post_comment") as post:
                code, result = self.check("--change", "org/repo#1", "--post")
                self.assertEqual(code, 0, result)
                self.assertEqual(result["source"], project.change_url(1))
                self.assertEqual([call.args[0] for call in read.call_args_list],
                                 ["change", "change-files", "change-with-description"])
                self.assertTrue(all(call.args[1] == project and call.kwargs["numbers"] == [1]
                                    for call in read.call_args_list))
                self.assertEqual(post.call_args.args[:3], (project, 1, self.head))

    def test_forge_read_rejects_missing_or_mismatched_file_head(self):
        project, (first, files, last) = self.forge_fixture("github")
        for file_head in ("b" * 40, None):
            with self.subTest(file_head=file_head):
                result = files.copy()
                if file_head is None:
                    del result["headSha"]
                else:
                    result["headSha"] = file_head
                with patch.object(source, "run_forge", side_effect=[first, result, last]):
                    with self.assertRaisesRegex(source.ReviewError, "Forge files do not match the reviewed head"):
                        source.forge_change("org/repo#1", project=project)

    def test_optional_forge_description_is_head_bound_and_best_effort(self):
        project, (first, files, last) = self.forge_fixture("github")
        described = dict(last, title=" Verify acceptance criteria ", body=" The new state is visible. ")
        with patch.object(source, "run_forge", side_effect=[first, files, described]) as read:
            change = source.forge_change("org/repo#1", project=project, with_description=True)
        self.assertEqual((change.title, change.description),
                         ("Verify acceptance criteria", "The new state is visible."))
        self.assertEqual(read.call_args.args[0], "change-with-description")

        with patch.object(source, "run_forge", side_effect=[first, files, source.ReviewError("unsupported"), last]):
            change = source.forge_change("org/repo#1", project=project, with_description=True)
        self.assertEqual(change.description, "")

        moved = dict(described, headSha="b" * 40)
        with patch.object(source, "run_forge", side_effect=[first, files, moved]):
            with self.assertRaisesRegex(source.ReviewError, "Change moved during the forge read"):
                source.forge_change("org/repo#1", project=project, with_description=True)

    def test_failed_post_reports_reason(self):
        project, reads = self.forge_fixture("github")
        reason = "pr-agent-forge comment exited 1: reviewed head differs from current head"
        with patch.object(cli, "load_config", return_value={project.key: project}), \
             patch.object(cli, "forge_credentials"), \
             patch.object(source, "run_forge", side_effect=reads), \
             patch.object(cli, "post_comment", side_effect=source.ReviewError(reason)):
            code, report = self.check("--change", "org/repo#1", "--post")
        self.assertEqual((code, report["publication"]), (2, "failed"))
        self.assertEqual(report["publication_error"], reason)

    def test_failed_cursor_keeps_scans(self):
        self.shim.write_text("#!/bin/sh\nexit 2\n")
        code,report = self.check("--base",self.base)
        self.assertEqual((code,report["state"]),(2,"model-unavailable"))
        self.assertEqual(report["cursor_invocations"],1)
        self.assertTrue(report["checks"][0]["findings"])
        self.assertFalse(list((self.root/"cache").glob("*.json")))

    def test_missing_patch_and_stale_artifact(self):
        change = source.Change(self.head,[source.File("image","", "added",True)],"org/repo#1")
        project, _ = self.forge_fixture("github")
        with patch.object(cli, "load_config", return_value={project.key: project}), \
             patch.object(cli, "forge_credentials"), patch.object(cli, "forge_change", return_value=change):
            code,report = self.check("--change","org/repo#1")
        self.assertEqual((code,report["omitted"]),(2,["image"]))
        self.assertFalse(self.calls.exists())
        report = self.root/"go.json"
        report.write_text(json.dumps({"schema":"go-analysis/v1","source_sha":self.base}))
        with self.assertRaises(source.ReviewError):
            cli.go_report(report,self.head)

    def test_invalid_location(self):
        info = {"review":{"key_issues_to_review":[{"relevant_file":"x","start_line":17,"end_line":17}]}}
        with self.assertRaises(source.ReviewError):
            engine.parse_answer(json.dumps({"type":"result","subtype":"success","is_error":False,
                                           "result":json.dumps(info)}),{"x":{1}})

    def test_auto_routing_preamble(self):
        result = '> Auto routed to Example Model\n\n{"review":{"key_issues_to_review":[]}}'
        usage = {"inputTokens": 4, "cacheReadTokens": 66}
        raw = json.dumps(dict(type="result", subtype="success", is_error=False, result=result, usage=usage))
        self.assertEqual(engine.parse_answer(raw, {}), {"findings": [], "cursor_usage": usage})
