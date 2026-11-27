import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

import httpx

from cursor_pr import artifacts, cli, engine, source


HEAD = "a" * 40
SLUG = "org/project"
NAME = f"go-analysis-lint-{HEAD}-20-2"


def fixture_report():
    finding = dict(path="logic.go", line=1, analyzer="staticcheck", rule="SA4006", disposition="new")
    return dict(schema="go-analysis/v1", repository=SLUG, source_sha=HEAD, state="complete", errors=[],
                expected_scans=1, coverage=[{"state": "complete"}], gate="fail", exit_code=1,
                findings=[finding, finding | {"path": "unrelated.go", "rule": "unrelated-rule"}],
                totals=dict(new=2, worsened=0, existing=0, resolved=0))


def archive(report):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as file:
        file.writestr("report.json", json.dumps(report))
    return data.getvalue()


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.start(patch.dict(os.environ, {"GITLAB_API_BASE": "https://gitlab.example/api/v4"}))
        self.lookup = self.start(patch.object(artifacts, "reviewer_token", return_value="fixture"))
        client = httpx.Client
        transport = httpx.MockTransport(self.respond)
        self.start(patch.object(artifacts.httpx, "Client", side_effect=lambda **opts: client(transport=transport, **opts)))
        self.report = fixture_report()
        self.expired = False
        self.pipeline_project = 3
        self.job_status = "failed"
        self.invalid_zip = False

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def respond(self, request):
        path = request.url.path
        host = request.url.host
        if host == "storage.example":
            self.assertNotIn("Authorization", request.headers)
            self.assertNotIn("PRIVATE-TOKEN", request.headers)
            if path == "/github":
                return httpx.Response(200, content=b"bad" if self.invalid_zip else archive(self.report))
            self.assertEqual(path, "/gitlab")
            return httpx.Response(200, json=self.report)
        if host == "api.github.com":
            self.assertEqual(request.headers["Authorization"], "Bearer fixture")
            base = "/repos/org/project/actions"
            if path == base + "/workflows/go.yml/runs":
                self.assertEqual(request.url.params["head_sha"], HEAD)
                run = dict(id=20, run_attempt=2, head_sha=HEAD, status="completed",
                           conclusion="failure", pull_requests=[{"number": 7}])
                return httpx.Response(200, json={"workflow_runs": [run]})
            if path == base + "/runs/20/artifacts":
                self.assertEqual(request.url.params["name"], NAME)
                return httpx.Response(200, json={"artifacts": [dict(name=NAME, id=30, expired=self.expired)]})
            self.assertEqual(path, base + "/artifacts/30/zip")
            return httpx.Response(302, headers={"Location": "https://storage.example/github"})
        self.assertEqual(host, "gitlab.example")
        self.assertEqual(request.headers["PRIVATE-TOKEN"], "fixture")
        base = "/api/v4/projects/org/project"
        if path == base + "/merge_requests/7":
            return httpx.Response(200, json=dict(sha=HEAD, target_project_id=3,
                head_pipeline=dict(id=22, project_id=self.pipeline_project)))
        if path == base + "/pipelines/22/jobs":
            if request.url.params["page"] == "1":
                return httpx.Response(200, json=[dict(id=88, name="different")], headers={"X-Next-Page":"2"})
            return httpx.Response(200, json=[dict(id=23, name="assay-go", status=self.job_status)])
        self.assertEqual(path, base + "/jobs/23/artifacts/report.json")
        return httpx.Response(302, headers={"Location": "https://storage.example/gitlab"})

    def exercise(self, provider, *, budget=6000):
        project, suffix, producer = ("https://github.com", "/pull/7", "go.yml") if provider == "github" else (
                                    "https://gitlab.example", "/-/merge_requests/7", "assay-go")
        url = project + "/" + SLUG + suffix
        change = source.Change(HEAD, [source.File("logic.go", "@@ -0,0 +1 @@\n+package app\n", "added")], url)
        args = cli.parser().parse_args(["--change", SLUG+"#7", "--expected-change-url", url,
            "--go-analysis-producer", producer, "--repo", str(self.root), "--post",
            "--cache-dir", str(self.root / "cache"), "--max-input-tokens", str(budget)])
        posts = []
        with patch.object(cli, "forge_change", return_value=change), \
             patch.object(cli, "post_comment", side_effect=lambda *args, **kwargs: posts.append(args[3])):
            report, status = cli.run_request(args)
        self.assertEqual(len(posts), 1)
        self.lookup.assert_called_with(provider, SLUG, self.root)
        return report, status, posts[0]

    def test_both_forges_import_and_publish_with_one_model_call(self):
        answer = {"findings": [], "cursor_usage": None}
        for forge in ("github", "gitlab"):
            with self.subTest(forge=forge), patch.object(engine, "invoke", return_value=answer) as model:
                report, status, post = self.exercise(forge)
                self.assertEqual((status, report["cursor_invocations"]), (0, 1))
                self.assertIn("` logic.go `:1 — ` SA4006 `", post)
                prompt = model.call_args.args[0]
                self.assertIn("SA4006", prompt)
                self.assertNotIn("unrelated-rule", prompt + post)
                self.assertTrue(self.exercise(forge)[0]["cache_hit"])
                self.assertEqual(self.exercise(forge, budget=1)[0]["state"], "over-budget")
                model.assert_called_once()

    def test_unavailable_evidence_is_partial_and_other_review_still_posts(self):
        for provider, attr, value in [
                ("github", "expired", True), ("github", "invalid_zip", True),
                ("gitlab", "pipeline_project", 99), ("gitlab", "job_status", "running"),
                ("gitlab", "report", fixture_report() | {"source_sha": "0"*40}),
                ("github", "report", fixture_report() | {"repository": "other/repo"}),
                ("github", "report", fixture_report() | {"expected_scans": 2})]:
            with self.subTest(provider=provider, attr=attr), patch.object(self, attr, value), \
                 patch.object(engine, "invoke", return_value={"findings": [], "cursor_usage": None}):
                result, status, post = self.exercise(provider)
                self.assertEqual((status, result["state"], result["checks"][0]["state"]),
                                 (2, "partial", "could-not-check"))
                self.assertIn("go-analysis: could not check", post)
                self.assertNotIn("SA4006", post)
                self.assertNotIn("fixture", post)
