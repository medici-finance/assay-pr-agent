from contextlib import closing
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from cursor_pr import history
from cursor_pr.config import load_config, project_from
from cursor_pr.discovery import is_request, public_gitlab_request
from cursor_pr.poll import Jobs, cycle, run_review


class PollTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.gh, self.gl = [
            project_from(dict(provider=p, url=f"https://{p}.com/acme/{p}", requester_ids=[7],
                              checkout=tmp.name)) for p in ("github", "gitlab")]
        self.projects = {p.key: p for p in (self.gh, self.gl)}
        self.note = dict(authorId=7, databaseId=81, authorType="User", body="/review",
                         createdAt="2026-11-15T11:11:00Z", updatedAt="2026-11-15T11:11:00Z",
                         url=self.gh.change_url(3)+"#issuecomment-81")
        self.since = datetime(2026, 11, 15, tzinfo=timezone.utc)

    def test_request_filter(self):
        self.assertTrue(is_request(self.gh, 3, self.note, self.since))
        for change in (dict(authorId=True), dict(authorId=8), dict(databaseId=-1), dict(authorType="Bot"),
                       dict(url=self.gl.change_url(3)), dict(body="> /review"), dict(body="/review please"),
                       dict(body=""), dict(updatedAt="later"), dict(createdAt=None), dict(updatedAt=None),
                       dict(createdAt="yesterday", updatedAt="yesterday")):
            with self.subTest(change=change):
                self.assertFalse(is_request(self.gh, 3, self.note | change, self.since))
        self.assertFalse(is_request(self.gh, 3, self.note, datetime(2027, 1, 1, tzinfo=timezone.utc)))

    def test_queued_and_interrupted_reviews_survive_a_restart(self):
        jobs = Jobs(self.state)
        since = jobs.activated(self.gh.key)
        jobs.enqueue(self.gh.key, 1, 10)
        jobs.enqueue(self.gh.key, 2, 11)
        self.assertEqual(jobs.next(), (self.gh.key, 1, 10))  # The poller stops before this review finishes.
        with self.assertLogs("cursor_pr.poll", level="WARNING"):
            restarted = Jobs(self.state)
        self.assertEqual(restarted.activated(self.gh.key), since)
        restarted.enqueue(self.gh.key, 1, 10)
        self.assertEqual(restarted.next(), (self.gh.key, 1, 10))
        self.assertEqual(restarted.next(), (self.gh.key, 2, 11))
        self.assertIsNone(restarted.next())

    def test_wizard_env_and_legacy_config(self):
        row = dict(provider="github", url=self.gh.url, checkout=str(self.state), requester_ids=[7])
        with patch.dict(os.environ, {}, clear=True), patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=["github",self.gh.url,str(self.state),"7","","n"]):
            initial = load_config(self.state)
            self.assertEqual(load_config(self.state), initial)
            config = self.state / "projects.json"
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            config.unlink()
            (self.state/"hooks.json").write_text(json.dumps(dict(public_url="ignored",
                projects=[row | {"secret":"legacy"}])))
            self.assertEqual(load_config(self.state), initial)
            self.assertNotIn("legacy", config.read_text())
            with patch.dict(os.environ, {"REVIEW_PROJECTS": json.dumps([
                    row | {"provider":"gitlab", "url":self.gl.url}])}):
                self.assertEqual(load_config(self.state), {self.gl.key:self.gl})

    def test_gitlab_candidate_visibility_and_credential_scope(self):
        token = self.state / "credential"
        token.write_text("fixture")
        record = dict(internal=False, system=False, id=81, noteable_type="MergeRequest", noteable_iid=3,
                      author={"id":7}, body="/review", created_at="t.001", updated_at="t.001")
        request = httpx.Request("GET", "https://gitlab.com")
        with patch.dict(os.environ, {"GITLAB_API_BASE":"https://gitlab.com/api/v4"}), \
             patch("cursor_pr.source.reviewer_token_file", return_value=token) as lookup, \
             patch("cursor_pr.discovery.httpx.get") as get:
            for delta in ({}, {"internal":True}, {"internal":None}, {"confidential":True}, {"system":True},
                          {"updated_at":"t.002"}, {"author":{"id":7,"bot":True}}, {"author":{"id":8}},
                          {"body":"changed"}, {"noteable_iid":4}):
                get.return_value = httpx.Response(200, json=record|delta, request=request)
                self.assertEqual(public_gitlab_request(self.gl,3,self.note), not delta)
            get.assert_called_with("https://gitlab.com/api/v4/projects/acme%2Fgitlab/merge_requests/3/notes/81",
                                   headers={"PRIVATE-TOKEN":"fixture"}, follow_redirects=False, timeout=15)
            lookup.assert_called_with("gitlab", self.gl.slug, self.gl.checkout)
            for status in (302, 403, 429, 503):
                get.return_value = httpx.Response(status, request=request)
                with self.assertRaises(httpx.HTTPStatusError):
                    public_gitlab_request(self.gl,3,self.note)
            lookup.reset_mock()
            with patch.dict(os.environ, {"GITLAB_API_BASE":"https://elsewhere.example/api/v4"}), \
                 self.assertRaisesRegex(Exception, "GITLAB_API_BASE"):
                public_gitlab_request(self.gl,3,self.note)
            lookup.assert_not_called()

    def test_polls_both_forges_on_unchanged_heads_and_recovers_after_partial_read(self):
        jobs = Jobs(self.state)
        since = {key: self.since for key in self.projects}
        notes, reviews = [], []
        broken = False
        def forge(verb, project, *, numbers=(), timeout):
            if verb == "open":
                return dict(numbers=[3], capped=False)
            self.assertEqual((verb, numbers), ("comments", (3,)))
            partial = broken and project.slug == self.gh.slug
            return dict(items=[] if partial else [dict(number=3, comments=notes)], partial=[3] if partial else [])
        def review(project, number, state):
            reviews.append((project.key,number))
            return "reviewed"
        with patch("cursor_pr.discovery.run_forge", side_effect=forge), \
             patch("cursor_pr.discovery.public_gitlab_request", return_value=True) as visibility:
            notes.append(self.note | dict(createdAt="2025-01-01T00:00:00Z", updatedAt="2025-01-01T00:00:00Z"))
            notes.append(self.note | dict(body="ordinary comment"))
            cycle(self.projects,self.state,jobs,since,review)
            self.assertEqual(reviews, [])
            visibility.assert_not_called()
            notes.append(self.note)
            broken = True
            cycle(self.projects,self.state,jobs,since,review)
            self.assertEqual(reviews, [(self.gl.key,3)])
            broken = False
            cycle(self.projects,self.state,Jobs(self.state),since,review)
            cycle(self.projects,self.state,Jobs(self.state),since,review)
            self.assertEqual(reviews, [(self.gl.key,3),(self.gh.key,3)])
            visibility.assert_called_once()

    def test_worker_passes_configured_ci_producer_to_review(self):
        from dataclasses import replace
        for project, producer in ((self.gh, "go.yml"), (self.gl, "go-lint")):
            with patch("cursor_pr.poll.supervised") as run:
                run.return_value = b'{"state":"reviewed","publication":"posted-or-already-present"}'
                self.assertEqual(run_review(replace(project, go_analysis=producer), 3, self.state), "reviewed")
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index("--go-analysis-producer")+1], producer)
                self.assertEqual(argv[argv.index("--expected-change-url")+1], project.change_url(3))

    def test_diagnostics_use_the_same_request_checks_and_keep_the_tier(self):
        job = Jobs(self.state)
        notes = [self.note | {"body": "/review-dropped mid"},
                 self.note | {"body": "/review-dropped", "authorId": 8},
                 self.note | {"body": "/review-dropped max", "authorType": "Bot"}]
        for project in (self.gh, self.gl):
            with self.subTest(project=project.provider), \
                 patch("cursor_pr.discovery.open_changes", return_value=([3], True)), \
                 patch("cursor_pr.discovery.comment_threads", return_value=({3: notes}, True)), \
                 patch("cursor_pr.discovery.public_gitlab_request", return_value=project == self.gh), \
                 patch("cursor_pr.poll.supervised") as run, \
                 patch.dict(os.environ, {"REVIEW_MAX_INPUT_TOKENS": "4000", "REVIEW_MAX_FINDINGS_MID": "3"}):
                run.return_value = b'{"state":"findings-dropped","publication":"posted-or-already-present"}'
                cycle({project.key: project}, self.state, job, {project.key: self.since})
                if project == self.gl:
                    run.assert_not_called()
                    continue
                run.assert_called_once()
                argv = run.call_args.args[0]
                self.assertIn("--explain-dropped", argv)
                self.assertEqual(argv[argv.index("--tier") + 1], "mid")
                self.assertEqual(argv[argv.index("--max-input-tokens") + 1], "4000")
                self.assertEqual(argv[argv.index("--max-findings") + 1], "3")
                self.assertEqual(job.tier((project.key, 3, 81)), "dropped-mid")

    def test_publication_failure_reason_is_logged(self):
        with patch("cursor_pr.poll.supervised") as run:
            run.return_value = json.dumps({
                "state": "reviewed", "publication": "failed",
                "publication_error": "pr-agent-forge comment exited 1: publication scope changed"}).encode()
            with self.assertLogs("cursor_pr.poll", level="WARNING") as logs:
                self.assertEqual(run_review(self.gh, 3, self.state), "failed")
        self.assertIn("pr-agent-forge comment exited 1: publication scope changed", "\n".join(logs.output))

    def test_real_command_path_with_stubbed_executables(self):
        # Only the external forge and model binaries are replaced. Exercise the installed
        # isolated CLI subprocess, parser, prompt, cache, publication and head checks.
        bindir=self.state/"bin"
        bindir.mkdir()
        def install(name, body):
            p=bindir/name
            p.write_text("#!/usr/bin/env python3\n"+body)
            p.chmod(0o755)
        install("pr-agent-forge", """
import json,os,sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
verb=sys.argv[1]
req=json.load(sys.stdin)
if verb=="mint":
    assert req["Repository"] == os.environ["FIXTURE_SLUG"].split("/")[1]
    print(json.dumps(dict(token="fixture", expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())))
    raise SystemExit(0)
assert req["Repository"]==os.environ["FIXTURE_SLUG"]
assert req["Token"] == "fixture"
assert "REVIEW_SECRET_FIXTURE" not in os.environ
if verb=="change":
    assert req["Numbers"]==[3]
    print(json.dumps(dict(url=os.environ["FIXTURE_CHANGE"],state="open",headSha="a"*40,changedFiles=1)))
elif verb=="change-files":
    print(json.dumps(dict(headSha="a"*40,complete=True,changedFiles=1,files=[dict(filename="demo.py",
        patchAbsent=False,status="added",patch="@@ -0,0 +1 @@\\n+x=1\\n")])))
else:
    assert verb=="comment"
    post=req["Publication"]
    assert post["Number"]==3 and post["Head"]=="a"*40
    if not post["AlreadyPosted"]:
        with Path(os.environ["POST_LOG"]).open("a") as out:
            out.write(post["Body"])
    print("null")
""")
        calls=self.state/"model-calls"
        install("cursor-agent",f"""
from pathlib import Path
import json
with Path({str(calls)!r}).open('a') as f: f.write('called\\n')
print(json.dumps(dict(type='result',subtype='success',is_error=False,
                      result='{{"review":{{"key_issues_to_review":[]}}}}')))
""")
        # Real Qualgen, local Git, and the installed poller child exercise both providers.
        checkout = self.state / "checkout"
        checkout.mkdir()
        def git(*args):
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org",
                            *args], cwd=checkout, check=True, capture_output=True)
        git("init", "-q")
        for revision in range(4):
            for name in ("demo.py", "partner.py"):
                (checkout / name).write_text(f"value = {revision}\n")
            git("add", "--", "demo.py", "partner.py")
            git("commit", "-qm", str(revision))
        posts=self.state/"posts"
        (self.state/"github.pem").write_text("fixture")
        (self.state/"gitlab.token").write_text("fixture")
        with patch.dict(os.environ,dict(PATH=str(bindir)+os.pathsep+os.environ["PATH"],
                    POST_LOG=str(posts), REVIEW_SECRET_FIXTURE="private", REVIEW_FORGE_DIR=str(self.state),
                    REVIEW_FORGE_APP_ID="1", REVIEW_FORGE_INSTALL_ID="2")):
            from dataclasses import replace
            for project in self.projects.values():
                project = replace(project, checkout=checkout)
                history.refresh(checkout, history.store_for(self.state, project))
                with patch.dict(os.environ, {"FIXTURE_CHANGE": project.change_url(3), "FIXTURE_SLUG": project.slug,
                                             "GITLAB_API_BASE": "https://gitlab.com/api/v4"}):
                    with patch.dict(os.environ, {"REVIEW_QUALGEN_INTERVAL_SECONDS": "0"}):
                        self.assertEqual(run_review(project,3,self.state),"reviewed")
                    # Advisories are part of the prompt, so enabling Qualgen changes the cache key: one more call.
                    # Started from the queue this time: the real child keeps its request's row.
                    jobs = Jobs(self.state)
                    jobs.enqueue(project.key, 3, 81)
                    self.assertEqual(jobs.next(), (project.key, 3, 81))
                    self.assertEqual(run_review(project,3,self.state,comment=81),"reviewed")
                    with closing(sqlite3.connect(jobs.path)) as db:
                        self.assertEqual(db.execute("SELECT status, attempts, head FROM requests WHERE project=?",
                                                    (project.key,)).fetchall(), [("reviewed", 1, "a"*40)])
                    jobs.set_tier(project.key, 3, 82, "dropped-regular")
                    jobs.enqueue(project.key, 3, 82)
                    request = jobs.next()
                    self.assertEqual(request, (project.key, 3, 82))
                    self.assertEqual(run_review(project, 3, self.state, jobs.tier(request), comment=82), "reviewed")
                    with patch.dict(os.environ,{"REVIEW_MAX_INPUT_TOKENS":"1"}):
                        self.assertEqual(run_review(project,3,self.state),"over-budget")
            with patch.dict(os.environ,{"FIXTURE_CHANGE":self.gl.change_url(3),"FIXTURE_SLUG":self.gh.slug}):
                self.assertEqual(run_review(self.gh,3,self.state),"failed")
        self.assertEqual(posts.read_text().count("historically coupled to partner.py"), 6)
        self.assertEqual(calls.read_text().splitlines(),["called"] * 6)
        self.assertEqual(posts.read_text().count("Rejection details for request 82."), 2)
        self.assertEqual(posts.read_text().count("No findings failed the citation or quote checks in this rerun."), 2)
        self.assertEqual(posts.read_text().count("Skipped Cursor"),2)
        self.assertEqual(posts.read_text().count("Review by [Assay PR Agent](https://pr-agent.assay.guide/)."), 8)
        self.assertNotIn("the-pr-agent", posts.read_text())
