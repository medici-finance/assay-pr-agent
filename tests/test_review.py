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
            "assert 'SC2086' in sys.argv[-1] and 'example.sh' in sys.argv[-1]\n"
            "assert 'GH_TOKEN' not in os.environ and not pathlib.Path('.git').exists()\n"
            f"with open({str(self.calls)!r}, 'a') as f: f.write('call\\n')\n"
            f"print({json.dumps(envelope)!r})\n")
        self.shim.chmod(0o700)
        env = patch.dict(os.environ, {"PATH":str(self.root)+os.pathsep+os.environ["PATH"],"GH_TOKEN":"fixture",
                                      "REVIEW_STATE_DIR":str(self.root/"state")})
        env.start()
        self.addCleanup(env.stop)
        # Tests exercise the saved and default budgets; a shell REVIEW_MAX_INPUT_TOKENS would win over both.
        os.environ.pop("REVIEW_MAX_INPUT_TOKENS", None)

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
        self.git("commit", "--allow-empty", "-qm", "same patch with new head")
        self.assertTrue(self.check(*args)[1]["cache_hit"])
        self.assertFalse(self.check(*args, "--model", "another-model")[1]["cache_hit"])
        self.assertEqual(self.calls.read_text().splitlines(), ["call", "call"])

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
                self.assertEqual([call.args[0] for call in read.call_args_list], ["change", "change-files", "change"])
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
