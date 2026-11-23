import json
import os
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

    def test_queued_survives_restart_and_started_is_never_reissued(self):
        jobs = Jobs(self.state)
        since = jobs.activated(self.gh.key)
        jobs.enqueue(self.gh.key, 1, 10)
        jobs.enqueue(self.gh.key, 2, 11)
        self.assertEqual(jobs.next(), (self.gh.key, 1, 10))
        restarted = Jobs(self.state)
        self.assertEqual(restarted.activated(self.gh.key), since)
        restarted.enqueue(self.gh.key, 1, 10)
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
             patch("cursor_pr.source.run_desk", return_value=str(token).encode()) as lookup, \
             patch("cursor_pr.discovery.httpx.get") as get:
            for delta in ({}, {"internal":True}, {"internal":None}, {"confidential":True}, {"system":True},
                          {"updated_at":"t.002"}, {"author":{"id":7,"bot":True}}, {"author":{"id":8}},
                          {"body":"changed"}, {"noteable_iid":4}):
                get.return_value = httpx.Response(200, json=record|delta, request=request)
                self.assertEqual(public_gitlab_request(self.gl,3,self.note), not delta)
            get.assert_called_with("https://gitlab.com/api/v4/projects/acme%2Fgitlab/merge_requests/3/notes/81",
                                   headers={"PRIVATE-TOKEN":"fixture"}, follow_redirects=False, timeout=15)
            self.assertIn("--no-rotate", lookup.call_args.args[0])
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
        def desk(args, *, cwd, timeout, env=None):
            self.assertEqual(cwd, self.state)
            if args[0] == "deskmonitor":
                self.assertEqual(args[:2], ["deskmonitor","pr"])
                root = Path(env["PR_MONITOR_STATE_DIR"])
                root.mkdir(parents=True, exist_ok=True)
                (root/(args[2].replace("/","__")+".state")).write_text("3 abc ready OPEN CLEAN\n")
                return b""  # No push, review or lifecycle event.
            self.assertEqual(args[:3], ["deskread","comments","--change"])
            slug = args[3].split("#")[0]
            item = dict(repo=slug,number=3,comments=notes)
            partial = broken and slug == self.gh.slug
            return json.dumps(dict(schema=1,kind="comments",items=[] if partial else [item],
                                   partial=[{"repo":slug,"number":3}] if partial else [])).encode()
        def review(project, number, state):
            reviews.append((project.key,number))
            return "reviewed"
        with patch("cursor_pr.discovery.run_desk", side_effect=desk), \
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
            with patch("cursor_pr.poll.subprocess.run") as run:
                run.return_value.stdout = b'{"state":"reviewed","publication":"posted-or-already-present"}'
                self.assertEqual(run_review(replace(project, go_analysis=producer), 3, self.state), "reviewed")
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index("--go-analysis-producer")+1], producer)
                self.assertEqual(argv[argv.index("--expected-change-url")+1], project.change_url(3))

    def test_real_command_path_with_stubbed_executables(self):
        # Only the external forge and model binaries are replaced. Exercise the installed
        # isolated CLI subprocess, parser, prompt, cache, publication and head checks.
        bindir=self.state/"bin"
        bindir.mkdir()
        def install(name, body):
            p=bindir/name
            p.write_text("#!/usr/bin/env python3\n"+body)
            p.chmod(0o755)
        install("deskread", """
import json,os,sys
kind=sys.argv[1]
assert sys.argv[2:]==['--change',os.environ['FIXTURE_SLUG']+'#3']
item=dict(repo=os.environ['FIXTURE_SLUG'],number=3)
if kind=='change':
    item['change']=dict(url=os.environ['FIXTURE_CHANGE'],state='OPEN',headSha='a'*40,changedFiles=1)
else:
    item['changeFiles']=dict(complete=True,changedFiles=1,files=[dict(filename='demo.py',
        patchAbsent=False,status='added',patch='@@ -0,0 +1 @@\\n+x=1\\n')])
print(json.dumps(dict(schema=1,kind=kind,items=[item],partial=[])))
""")
        # Standalone PR/MR comments must never pass through issue filing or PR creation.
        unexpected=self.state/"unexpected-desk-command"
        for tool in ("deskfile", "deskpr"):
            install(tool, f"from pathlib import Path\nPath({str(unexpected)!r}).touch()\nraise SystemExit(1)")
        calls=self.state/"model-calls"
        install("cursor-agent",f"""
from pathlib import Path
import json
with Path({str(calls)!r}).open('a') as f: f.write('called\\n')
print(json.dumps(dict(type='result',subtype='success',is_error=False,
                      result='{{"review":{{"key_issues_to_review":[]}}}}')))
""")
        install("deskpost","""
import os,sys
from pathlib import Path
assert sys.argv[1:8]==['comment',os.environ['FIXTURE_SLUG'],'3','--kind','mr','--head','a'*40]
assert 'REVIEW_SECRET_FIXTURE' not in os.environ
with Path(os.environ['POST_LOG']).open('a') as out:
    out.write(Path(sys.argv[-1]).read_text())
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
        with patch.dict(os.environ,dict(PATH=str(bindir)+os.pathsep+os.environ["PATH"],
                    POST_LOG=str(posts), REVIEW_SECRET_FIXTURE="private")):
            from dataclasses import replace
            for project in self.projects.values():
                project = replace(project, checkout=checkout)
                history.refresh(checkout, history.store_for(self.state, project))
                with patch.dict(os.environ,{"FIXTURE_CHANGE":project.change_url(3),"FIXTURE_SLUG":project.slug}):
                    with patch.dict(os.environ, {"REVIEW_QUALGEN_INTERVAL_SECONDS": "0"}):
                        self.assertEqual(run_review(project,3,self.state),"reviewed")
                    # The cache key contains the exact prompt. Enabling Qualgen must hit the same key.
                    self.assertEqual(run_review(project,3,self.state),"reviewed")
                    with patch.dict(os.environ,{"REVIEW_MAX_INPUT_TOKENS":"1"}):
                        self.assertEqual(run_review(project,3,self.state),"over-budget")
            with patch.dict(os.environ,{"FIXTURE_CHANGE":self.gl.change_url(3),"FIXTURE_SLUG":self.gh.slug}):
                self.assertEqual(run_review(self.gh,3,self.state),"failed")
        self.assertEqual(posts.read_text().count("historically coupled to partner.py"), 4)
        self.assertFalse(unexpected.exists())
        self.assertEqual(calls.read_text().splitlines(),["called","called"])
        self.assertEqual(posts.read_text().count("Skipped Cursor"),2)
        self.assertEqual(posts.read_text().count("[PR-Agent](https://github.com/the-pr-agent/pr-agent)"),6)
