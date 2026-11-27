import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import httpx

from cursor_pr import artifacts, cli, engine, scanners, source


DOCKER = "services/web/Dockerfile"
WORKFLOW = ".github/workflows/check.yml"
LOCK = "deps/requirements.txt"
SLUG = "org/repo"


class SummaryScannerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.start(patch.dict(os.environ, {"REVIEW_STATE_DIR": str(self.root / "state"),
                   "REVIEW_OSV_DOWNLOAD_DATABASES": "0", "GITLAB_API_BASE": "https://gitlab.test/api/v4"}))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        contents = {DOCKER: "FROM ubuntu:latest\n", LOCK: "fixturelib==1.0.0\n",
                    WORKFLOW: "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"}
        self.files = []
        for path, body in contents.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
            self.files.append(source.File(path, "@@ -0,0 +1 @@\n+" + body.splitlines()[0] + "\n", "added"))
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c",
                        "user.email=t@example.org", "commit", "-qm", "fixture"], check=True)
        self.head = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        self.ci_head = self.head
        self.event = "push"
        self.stamp = None
        self.raw = [{"file": DOCKER, "code": "DL3007", "line": 1, "message": "CI-only marker"}]
        self.start(patch.object(artifacts, "reviewer_token", return_value="fixture"))
        client = httpx.Client
        self.start(patch.object(artifacts.httpx, "Client", side_effect=lambda **opts: client(
            transport=httpx.MockTransport(self.respond), **opts)))

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def respond(self, request):
        p = request.url.path
        if p.endswith("/workflows/hadolint.yml/runs"):
            self.assertEqual(request.url.params["head_sha"], self.head)
            return httpx.Response(200, json={"workflow_runs":[dict(head_sha=self.ci_head, status="completed",
                conclusion="success", event=self.event, id=5, run_attempt=1, pull_requests=[{"number":7}])]})
        if p.endswith("/runs/5/artifacts"):
            return httpx.Response(200, json={"artifacts":[{"name":"hadolint", "id":12, "expired":False}]})
        if p.endswith("/artifacts/12/zip"):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as z:
                z.writestr("hadolint.json", json.dumps(self.raw))
                if self.stamp:
                    z.writestr("review-head.txt", self.stamp + "\n")
            return httpx.Response(200, content=data.getvalue())
        if p.endswith("/merge_requests/7"):
            return httpx.Response(200, json={"sha":self.head, "target_project_id":2,
                "head_pipeline":{"id":5, "project_id":2, "sha":self.ci_head}})
        if p.endswith("/pipelines/5/jobs"):
            return httpx.Response(200, json=[{"id":12, "status":"success", "name":"hadolint",
                                              "commit":{"id":self.ci_head}}])
        if p.endswith("/jobs/12/artifacts/hadolint.json"):
            return httpx.Response(200, json=self.raw)
        return httpx.Response(404)

    @property
    def urls(self):
        return ("https://github.com/org/repo/pull/7", "https://gitlab.test/org/repo/-/merge_requests/7")

    def test_ci_exact_head_and_scope(self):
        wanted = {("hadolint", DOCKER), ("hadolint", "unrelated/Dockerfile")}
        for url in self.urls:
            with self.subTest(url=url):
                self.ci_head = self.head
                self.raw[0]["file"] = ("/builds/org/repo/" if "gitlab" in url else "/home/runner/work/repo/repo/") + DOCKER
                original = self.raw
                got = scanners.ci_results(url, self.head, self.root, wanted, {})
                self.assertEqual(set(got), {("hadolint", DOCKER)})
                self.raw = []
                self.assertFalse(scanners.ci_results(url, self.head, self.root, wanted, {}))
                all_paths = {"hadolint": {"all_paths": True}}
                self.assertEqual(set(scanners.ci_results(url, self.head, self.root, wanted, all_paths)), wanted)
                self.ci_head = "a" * 40
                self.assertFalse(scanners.ci_results(url, self.head, self.root, wanted, all_paths))
                self.raw = {"malformed": True}
                self.ci_head = self.head
                self.assertFalse(scanners.ci_results(url, self.head, self.root, wanted, all_paths))
                self.raw = original

    def test_github_pr_checkout_must_attest_source_sha(self):
        url = self.urls[0]
        wanted = {("hadolint", DOCKER)}
        self.event = "pull_request"
        for stamp in (None, "e" * 40, self.head):
            with self.subTest(stamp=stamp):
                self.stamp = stamp
                reused = scanners.ci_results(url, self.head, self.root, wanted, {})
                self.assertEqual(bool(reused), stamp == self.head)

    def test_every_native_ci_format_on_both_forges(self):
        for url in self.urls:
            prefix = "/builds/org/repo/" if "gitlab" in url else "/home/runner/work/repo/repo/"
            reports = {
                "hadolint": self.raw,
                "zizmor": {"version":"2.1.0","runs":[{"tool":{"driver":{"name":"zizmor"}},
                    "results":[{"ruleId":"zizmor/unpinned-uses","message":{"text":"pin this action"},
                        "locations":[{"physicalLocation":{"artifactLocation":{"uri":WORKFLOW},
                                                         "region":{"startLine":6}}}]}]}]},
                "osv-scanner": {"results":[{"source":{"path":prefix + LOCK}, "packages":[{
                    "package":{"name":"fixturelib","version":"1.0.0"},
                    "vulnerabilities":[{"id":"OSV-FIXTURE-1"}]}]}]},
            }
            def fetch(url, producer, head, root, *, scanner):
                self.assertEqual(head, self.head)
                return json.dumps(reports[scanner["artifact"]]), True
            with self.subTest(url=url), patch.object(scanners, "fetch_report", side_effect=fetch):
                checks = scanners.ci_results(url,self.head,self.root,scanners.inputs(self.files),{})
                self.assertEqual(set(checks), scanners.inputs(self.files))
                self.assertEqual(checks["zizmor",WORKFLOW]["findings"][0]["line"], 6)
                self.assertEqual(checks["osv-scanner",LOCK]["findings"][0]["message"], "fixturelib 1.0.0")

    def test_review_mixed_sources_without_changing_prompt_or_cache(self):
        calls, posts = [], []
        def invoke(prompt, model, anchors):
            calls.append(prompt)
            return {"findings": [], "model_usage": None}
        def publish(project, number, head, body, state, **kwargs):
            posts.append(body)
        def local(root, head, tool, path, cache, **kwargs):
            self.assertNotEqual(tool, "hadolint")
            return scanners.completed(tool, path, "local", [{"line": None, "rule":"LOCAL", "message":"local-marker"}])
        self.start(patch.object(engine, "invoke", side_effect=invoke))
        self.start(patch.object(cli, "post_comment", side_effect=publish))
        self.start(patch.object(cli, "scan_files", return_value=[]))
        self.start(patch.object(scanners, "local", side_effect=local))
        expected = engine.pack(self.files_sorted(), [])[0]
        for url in self.urls:
            with self.subTest(url=url), patch.object(cli, "forge_change", return_value=source.Change(self.head,self.files,url)):
                args = cli.parser().parse_args(["--change",SLUG+"#7","--expected-change-url",url,"--post",
                            "--repo",str(self.root),"--cache-dir",str(self.root/"cache"),"--max-input-tokens","6000"])
                with patch.object(scanners, "inputs", return_value=set()):
                    baseline, _ = cli.run_request(args)
                report, _ = cli.run_request(args)
                self.assertTrue(report["cache_hit"])
                self.assertEqual(report["model_invocations"], 0)
                self.assertEqual(report["estimated_input_tokens"], baseline["estimated_input_tokens"])
                self.assertIn("CI-only marker", posts[-1])
                self.assertIn("local-marker", posts[-1])
                args.max_input_tokens = 1
                bounded, _ = cli.run_request(args)
                self.assertEqual(bounded["state"], "over-budget")
                self.assertIn("CI-only marker", posts[-1])
        self.assertEqual(calls, [expected, expected])

    def files_sorted(self):
        return sorted(self.files, key=lambda item:item.path)

    @unittest.skipUnless(all(shutil.which(t) for t in scanners.TOOLS), "scanners shipped in image")
    def test_real_tools_offline_on_immutable_sources(self):
        cache = self.root / "osv-cache"
        db = cache/"osv-scalibr"/"PyPI"/"all.zip"
        db.parent.mkdir(parents=True)
        advisory = {"id":"OSV-FIXTURE-1","modified":"2025-01-01T00:00:00Z","affected":[
            {"package":{"ecosystem":"PyPI","name":"fixturelib"}, "versions":["1.0.0"]}]}
        with zipfile.ZipFile(db, "w") as z:
            z.writestr("OSV-FIXTURE-1.json", json.dumps(advisory))
        for tool, path, rule in (("hadolint", DOCKER, "DL3007"), ("zizmor", WORKFLOW, "zizmor/"),
                                 ("osv-scanner", LOCK, "OSV-FIXTURE-1")):
            with self.subTest(tool=tool):
                (self.root/path).write_text("working tree differs")
                result = scanners.local(self.root,self.head,tool,path,cache)
                self.assertEqual(result["state"], "complete", result)
                self.assertTrue(any(r["rule"].startswith(rule) for r in result["findings"]), result)
        db.unlink()
        self.assertEqual(scanners.local(self.root,self.head,"osv-scanner",LOCK,self.root/"missing")["state"],"could-not-check")
