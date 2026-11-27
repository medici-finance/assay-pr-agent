import contextlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import httpx

from cursor_pr import cli, engine, forge_status, source
from cursor_pr.config import project_from
from cursor_pr.scanners import fence

HEAD = "a" * 40
SLUG = "org/project"
NAME = "assay-pr-agent/review"
SITES = {"github": "https://github.com/" + SLUG + "/pull/7",
         "gitlab": "https://gitlab.example/" + SLUG + "/-/merge_requests/7"}
APIS = {"github": "https://api.github.com/repos/" + SLUG + "/check-runs",
        "gitlab": "https://gitlab.example/api/v4/projects/org%2Fproject/statuses/" + HEAD}
HOSTILE = "@all [click](https://evil.example) `x`"
STATES = (("reviewed", "success"), ("empty", "success"), ("partial", "failure"),
          ("over-budget", "failure"), ("model-unavailable", "failure"))


def outside_code_spans(text):
    """Drop the spans Markdown renders as code; only what remains is live Markdown."""
    return re.sub(r"(`+).*?\1", "", text, flags=re.S)


def finding(**changes):
    return {"path": "src/app.py", "line": 3, "end_line": 4, "title": "Unchecked input", "message": "Validate it."} | changes


def review(state="reviewed", findings=None):
    return {"provider": "cursor", "state": state, "head": HEAD, "findings": [finding()] if findings is None else findings,
            "checks": [{"tool": "shellcheck", "path": "run.sh", "state": "complete", "findings": [
                {"path": "run.sh", "rule": "SC2086", "line": 2, "message": "Quote it."}]},
                {"tool": "go-analysis", "state": "could-not-check", "findings": []}],
            "static_checks": [{"tool": "hadolint", "path": "Dockerfile", "state": "complete", "findings": [
                {"rule": "DL3007", "line": 1, "message": "Pin it."}]}],
            "omitted": ["docs/old.md"]}


class ForgeStatusPayloadTests(unittest.TestCase):
    def test_payloads_follow_the_report_state_on_both_forges(self):
        for state, result in STATES:
            with self.subTest(state=state):
                value = review(state)
                title, detail, head = forge_status.facts(value)
                run = forge_status.github(value, SITES["github"], title, detail, head)
                self.assertEqual((run["name"], run["head_sha"], run["status"], run["conclusion"], run["details_url"]),
                                 (NAME, HEAD, "completed", result, SITES["github"]))
                self.assertEqual(run["output"]["title"], f"{state}: 1 model finding")
                self.assertEqual(run["output"]["summary"], f"Assay PR Agent review of {HEAD}\n\n" + forge_status.SENTENCES[state]
                                 + " 1 model finding · 2 static and scanner findings · 1 check that could not run · 1 path outside the review")
                self.assertEqual(run["output"]["annotations"], [dict(
                    path="src/app.py", start_line=3, end_line=4, annotation_level="warning",
                    message="` Unchecked input `. ` Validate it. `")])
                status = forge_status.gitlab(value, SITES["gitlab"], title, detail)
                gitlab_state = "success" if result == "success" else "failed"
                self.assertEqual((status["name"], status["state"], status["target_url"]), (NAME, gitlab_state, SITES["gitlab"]))
                self.assertLessEqual(len(status["description"]), 255)
                self.assertTrue(status["description"].startswith(f"{state}: 1 model finding. {forge_status.SENTENCES[state]}"))

    def test_untrusted_text_reaches_only_annotations_and_only_as_code(self):
        long = finding(title="t" * 1000, message="m" * 5000)
        value = review("reviewed", [
            finding(path="@all", title=HOSTILE, message="[link](https://evil.example) ``` ``` @someone"),
            finding(path="/etc/passwd"), finding(path="../escape.py"), finding(line=0), finding(line=True),
            finding(title="", message=" \n"), finding(end_line=1), long])
        title, detail, head = forge_status.facts(value)
        run = forge_status.github(value, SITES["github"], title, detail, head)
        self.assertEqual(run["output"]["title"], "reviewed: 8 model findings")
        for text in (run["output"]["title"], run["output"]["summary"]):
            for marker in ("@all", "@someone", "evil.example", "app.py", "t" * 20):
                self.assertNotIn(marker, text)
        rows = run["output"]["annotations"]
        self.assertEqual([(r["path"], r["start_line"], r["end_line"], r["annotation_level"]) for r in rows],
                         [("@all", 3, 4, "warning"), ("src/app.py", 3, 3, "warning"), ("src/app.py", 3, 4, "warning")])
        self.assertEqual(rows[0]["message"], f"{fence(HOSTILE, 255)}. {fence('[link](https://evil.example) ``` ``` @someone', 700)}")
        live = outside_code_spans("".join(r["message"] for r in rows))
        for marker in ("@all", "@someone", "evil.example", "``", "tttt"):
            self.assertNotIn(marker, live)
        self.assertLessEqual(len(rows[2]["message"]), 255 + 700 + 12)
        status = forge_status.gitlab(value, SITES["gitlab"], title, detail)
        self.assertLessEqual(len(status["description"]), 255)

    def test_at_most_fifty_annotations(self):
        value = review("reviewed", [finding(line=n, end_line=n) for n in range(1, 61)])
        title, detail, head = forge_status.facts(value)
        rows = forge_status.github(value, SITES["github"], title, detail, head)["output"]["annotations"]
        self.assertEqual([r["start_line"] for r in rows], list(range(1, 51)))


class ForgeStatusRequestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.start(patch.dict(os.environ, {"GITLAB_API_BASE": "https://gitlab.example/api/v4",
                                           "REVIEW_STATE_DIR": str(self.root / "state")}))
        os.environ.pop("REVIEW_FORGE_STATUS", None)
        self.policy = self.start(patch.object(forge_status, "check_publication"))
        self.lookup = self.start(patch.object(forge_status, "reviewer_token", return_value="fixture"))
        self.requests, self.clients = [], []
        self.status, self.location, self.error = 201, None, None
        self.gitlab_source = dict(sha=HEAD, source_project_id=123, target_project_id=123, source_branch="topic/ref")
        client = httpx.Client
        transport = httpx.MockTransport(self.respond)
        def build(**options):
            self.clients.append(options)
            return client(transport=transport, **options)
        self.start(patch.object(forge_status.httpx, "Client", side_effect=build))

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def respond(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.location:
            return httpx.Response(302, headers={"Location": self.location})
        if request.method == "GET" and request.url.path.endswith("/merge_requests/7"):
            return httpx.Response(200, json=self.gitlab_source)
        return httpx.Response(self.status, json={"id": 1})

    def exercise(self, provider, *options, url=None, post=True, model=None, address=None, desk_error=None):
        change = source.Change(HEAD, [source.File("demo.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")], url or SITES[provider])
        args = cli.parser().parse_args(["--change", address or SLUG + "#7", "--repo", str(self.root), *(["--post"] if post else []),
                                         *options, "--cache-dir", str(self.root / "cache"), "--max-input-tokens", "6000"])
        slug = (address or SLUG + "#7").partition("#")[0]
        site = ("https://github.com" if provider == "github" else os.environ["GITLAB_API_BASE"].removesuffix("/api/v4"))
        args.forge_project = project_from(dict(provider=provider, url=f"{site}/{slug}", checkout=str(self.root),
                                               requester_ids=[7]))
        posts = []
        with patch.object(cli, "forge_change", return_value=change), \
             patch.object(cli, "post_comment", side_effect=desk_error or (lambda *call: posts.append(call))) as desk, \
             patch.object(engine, "invoke", return_value={"findings": [finding()], "cursor_usage": None}), \
             (patch.object(cli, "review", side_effect=model) if model else contextlib.nullcontext()):
            report, code = cli.run_request(args)
        return report, code, posts, desk

    def test_posts_one_check_run_or_one_commit_status(self):
        for provider in SITES:
            with self.subTest(provider=provider):
                self.requests.clear()
                report, code, posts, desk = self.exercise(provider, "--forge-status")
                self.assertEqual((code, report["forge_status"], report["publication"]), (0, "posted", "posted-or-already-present"))
                [request] = [request for request in self.requests if request.method == "POST"]
                self.assertEqual((request.method, str(request.url)), ("POST", APIS[provider]))
                body = json.loads(request.content)
                self.lookup.assert_called_with(provider, SLUG, self.root)
                if provider == "github":
                    self.assertEqual(request.headers["Authorization"], "Bearer fixture")
                    self.assertEqual((body["name"], body["head_sha"], body["conclusion"], body["details_url"]),
                                     (NAME, HEAD, "success", SITES[provider]))
                    self.assertEqual(body["output"]["annotations"][0]["message"], "` Unchecked input `. ` Validate it. `")
                else:
                    self.assertEqual(request.headers["PRIVATE-TOKEN"], "fixture")
                    self.assertEqual((body["name"], body["state"], body["target_url"]),
                                 (NAME, "success", SITES[provider]))
                    self.assertEqual(body["ref"], "topic/ref")
                self.assertEqual(desk.call_count, 1)
                self.assertEqual(desk.call_args.args[1:3], (7, HEAD))
                self.assertEqual(self.policy.call_args.args[1:3], (7, HEAD))
                self.assertNotIn("fixture", json.dumps(report))
        self.assertEqual(self.clients, [{"timeout": 15, "follow_redirects": False, "mounts": {}}] * 2)

    def test_private_ca_transport_is_used_for_the_gitlab_instance(self):
        transports = []
        with patch.object(forge_status, "client_options", return_value={"verify": False}), \
             patch.object(forge_status.httpx, "HTTPTransport", side_effect=lambda **opts: transports.append(opts) or httpx.MockTransport(self.respond)):
            report, *_ = self.exercise("gitlab", "--forge-status")
        self.assertEqual(report["forge_status"], "posted")
        [options] = self.clients
        self.assertEqual(list(options["mounts"]), ["https://gitlab.example"])
        self.assertEqual(transports, [{"verify": False}])
        self.assertEqual([r.method for r in self.requests], ["GET", "POST"])

    def test_flag_or_environment_variable_enable_it_and_otherwise_it_is_skipped(self):
        with patch.dict(os.environ, {"REVIEW_FORGE_STATUS": "1"}):
            self.assertEqual(self.exercise("github")[0]["forge_status"], "posted")
        self.assertEqual(len(self.requests), 1)
        self.requests.clear()
        with patch.dict(os.environ, {"REVIEW_FORGE_STATUS": "true"}):
            self.assertEqual(self.exercise("github")[0]["forge_status"], "skipped")
        report, code, *_ = self.exercise("github", "--forge-status", "--estimate", post=False)
        self.assertEqual((code, report["forge_status"], report["state"]), (0, "skipped", "estimate"))
        with patch.object(cli, "local_change", return_value=source.Change(HEAD, [source.File("demo.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")], str(self.root))):
            args = cli.parser().parse_args(["--base", "b" * 40, "--repo", str(self.root), "--forge-status",
                                            "--cache-dir", str(self.root / "cache"), "--max-input-tokens", "6000"])
            with patch.object(cli, "review", return_value=(dict(findings=[], cursor_usage=None), False)):
                report, code = cli.run_request(args)
        self.assertEqual((code, report["forge_status"]), (0, "skipped"))
        self.assertEqual(self.requests, [])

    def test_refusal_is_recorded_and_logged_without_changing_the_exit_code(self):
        for model in (None, engine.CursorFailure("Cursor CLI is not installed", 0)):
            with self.subTest(model_failed=bool(model)):
                self.status = 403
                with self.assertLogs("cursor_pr.forge_status", level="WARNING") as logs:
                    report, code, posts, _ = self.exercise("github", "--forge-status", model=model)
                self.assertEqual((code, report["forge_status"], report["state"], report["publication"]),
                                 (2 if model else 0, "failed", "model-unavailable" if model else "reviewed", "posted-or-already-present"))
                self.assertEqual([r.getMessage() for r in logs.records],
                                 ["Forge status for org/project#7 was not posted (HTTP 403)"])
                self.assertEqual(len(posts), 1)
                self.assertEqual(self.exercise("github", model=model)[1], code)
        self.error = httpx.ConnectError("https://gitlab.example/api/v4/private?token=fixture")
        with self.assertLogs("cursor_pr.forge_status", level="WARNING") as logs:
            report, *_ = self.exercise("gitlab", "--forge-status")
        self.assertEqual(report["forge_status"], "failed")
        self.assertEqual([r.getMessage() for r in logs.records], ["Forge status for org/project#7 was not posted (ConnectError)"])

    def test_redirect_is_never_followed_with_the_credential(self):
        self.location = "https://elsewhere.example/collect"
        with self.assertLogs("cursor_pr.forge_status", level="WARNING"):
            report, *_ = self.exercise("gitlab", "--forge-status")
        self.assertEqual((report["forge_status"], len(self.requests)), ("failed", 1))

    def test_a_change_url_that_disagrees_with_the_address_never_reaches_the_forge(self):
        for url in ("https://github.com/org/other/pull/7", "https://github.com/org/project/pull/8",
                    "https://github.com/org/project/pull/7?token=1", "https://user@github.com/org/project/pull/7",
                    "https://github.com/org/project/-/merge_requests/7", "https://evil.example/org/project/pull/7",
                    "https://github.com/org/project/pull/7\n", SLUG + "#7"):
            with self.subTest(url=url), self.assertLogs("cursor_pr.forge_status", level="WARNING"):
                report, *_ = self.exercise("github", "--forge-status", url=url)
            self.assertEqual(report["forge_status"], "failed")
        with self.assertLogs("cursor_pr.forge_status", level="WARNING"):
            self.assertEqual(self.exercise("gitlab", "--forge-status", url="https://gitlab.example/deep/group/project/-/merge_requests/7")[0]["forge_status"], "failed")
        self.assertEqual(self.requests, [])
        self.lookup.reset_mock()
        self.assertEqual(self.exercise("gitlab", "--forge-status", url="https://gitlab.example/org/project/-/merge_requests/7")[0]["forge_status"], "posted")
        with self.assertNoLogs("cursor_pr.forge_status", level="WARNING"):
            self.assertEqual(self.exercise("github", "--forge-status", url="https://github.com/Org/Project/pull/7", address="Org/Project#7")[0]["forge_status"], "posted")
        self.assertEqual(self.lookup.call_args.args, ("github", "Org/Project", self.root))
        self.assertEqual([str(r.url) for r in self.requests if r.method == "POST"],
                         ["https://gitlab.example/api/v4/projects/org%2Fproject/statuses/" + HEAD, "https://api.github.com/repos/Org/Project/check-runs"])

    def test_status_only_never_writes_when_the_publication_policy_refuses(self):
        self.policy.side_effect = source.ReviewError("repository visibility exceeds configured publication scope")
        with self.assertLogs("cursor_pr.forge_status", level="WARNING"):
            report, code, posts, _ = self.exercise("github", "--forge-status", post=False)
        self.assertEqual((code, report["forge_status"], posts), (0, "failed", []))
        self.assertEqual(self.requests, [])
        self.lookup.assert_not_called()
        self.assertIn("src/app.py", self.policy.call_args.args[3])

    def test_gitlab_check_uses_the_configured_installation_prefix(self):
        with patch.dict(os.environ, {"GITLAB_API_BASE": "https://git.example:8443/gitlab/api/v4"}):
            url = "https://git.example:8443/gitlab/org/project/-/merge_requests/7"
            report, *_ = self.exercise("gitlab", "--forge-status", url=url)
            self.assertEqual(report["forge_status"], "posted")
            self.assertEqual(str(self.requests[-1].url),
                             f"https://git.example:8443/gitlab/api/v4/projects/org%2Fproject/statuses/{HEAD}")

    def test_a_gitlab_instance_under_a_path_is_reached_at_its_api(self):
        site = "https://gitlab.example/gitlab/" + SLUG + "/-/merge_requests/7"
        with patch.dict(os.environ, {"GITLAB_API_BASE": "https://gitlab.example/gitlab/api/v4"}):
            self.assertEqual(self.exercise("gitlab", "--forge-status", url=site)[0]["forge_status"], "posted")
            # The project still has to sit under the instance's path, and the number still has to match.
            for url in ("https://gitlab.example/other/" + SLUG + "/-/merge_requests/7",
                        "https://gitlab.example/gitlab/" + SLUG + "/-/merge_requests/8"):
                with self.subTest(url=url), self.assertLogs("cursor_pr.forge_status", level="WARNING"):
                    self.assertEqual(self.exercise("gitlab", "--forge-status", url=url)[0]["forge_status"], "failed")
        self.assertEqual([str(r.url) for r in self.requests if r.method == "POST"],
                         ["https://gitlab.example/gitlab/api/v4/projects/org%2Fproject/statuses/" + HEAD])
        self.assertEqual(json.loads(self.requests[-1].content)["target_url"], site)

    def test_gitlab_status_refuses_a_fork_or_moved_head(self):
        for update in (dict(source_project_id=124), dict(sha="b" * 40)):
            with self.subTest(update=update), self.assertLogs("cursor_pr.forge_status", level="WARNING"):
                self.requests.clear()
                self.gitlab_source = dict(sha=HEAD, source_project_id=123, target_project_id=123, source_branch="topic/ref") | update
                report, *_ = self.exercise("gitlab", "--forge-status")
                self.assertEqual(report["forge_status"], "failed")
                self.assertEqual([request.method for request in self.requests], ["GET"])

    def test_a_failed_summary_publication_leaves_the_status_unattempted(self):
        report, code, *_ = self.exercise("github", "--forge-status", desk_error=source.ReviewError("pr-agent-forge comment exited 1"))
        self.assertEqual((code, report["publication"]), (2, "failed"))
        self.assertNotIn("forge_status", report)
        self.assertEqual(self.requests, [])

    def test_the_poller_child_inherits_the_environment_variable(self):
        from cursor_pr.config import project_from
        from cursor_pr.poll import run_review
        project = project_from(dict(provider="github", url="https://github.com/" + SLUG, requester_ids=[7], checkout=str(self.root)))
        with patch.dict(os.environ, {"REVIEW_FORGE_STATUS": "1"}), patch("cursor_pr.poll.supervised") as run:
            run.return_value = json.dumps({"state": "reviewed", "publication": "posted-or-already-present"}).encode()
            self.assertEqual(run_review(project, 7, self.root), "reviewed")
        self.assertEqual(run.call_args.kwargs["env"]["REVIEW_FORGE_STATUS"], "1")
        self.assertNotIn("--forge-status", run.call_args.args[0])
