import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from cursor_pr.hooks import Jobs, make_app, run_review
from cursor_pr.hook_config import load_config, project_from


class HookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.gh, self.gl = [
            project_from(dict(provider=p, url=f"https://{p}.com/acme/app", requester_ids=[7],
                              secret="fixture-token-" + p, checkout=tmp.name)) for p in ("github","gitlab")]
        self.projects = {p.key: p for p in (self.gh, self.gl)}
        self.gh_payload = dict(action="created", repository={"html_url": self.gh.url},
            issue=dict(number=3, state="open", pull_request={}), sender=dict(id=7, type="User"),
            comment=dict(id=81, body="/review", user=dict(id=7, type="User")))
        self.gl_payload = dict(object_kind="note", project=dict(web_url=self.gl.url, id=12),
            user=dict(id=7), merge_request=dict(iid=3, state="opened", target_project_id=12),
            object_attributes=dict(id=81, note="/review", author_id=7, action="create",
                system=False, project_id=12, noteable_type="MergeRequest"))

    def send(self, client, proj, data, event=None, valid=True):
        raw = json.dumps(data).encode()
        secret = proj.secret if valid else b"wrong"
        if proj.provider == "github":
            headers = {"x-hub-signature-256": "sha256="+hmac.new(secret,raw,hashlib.sha256).hexdigest(),
                       "x-github-event": event or "issue_comment"}
        else:
            headers = {"x-gitlab-token": secret.decode(), "x-gitlab-event": event or "Note Hook"}
        return client.post("/hooks/"+proj.key, content=raw, headers=headers).status_code

    def test_only_explicit_human_requests(self):
        def bad_run(*_):
            self.fail("Received an unexpected request")
        with TestClient(make_app(self.projects, self.state, bad_run)) as client:
            for project, payload in ((self.gh,self.gh_payload),(self.gl,self.gl_payload)):
                self.assertEqual(self.send(client, project, payload, valid=False),403)
                self.assertEqual(self.send(client, project, payload, event="push"),204)
            for path, value in (
                ("action","edited"),("issue.state","closed"),("issue.pull_request",None),
                ("sender.type","Bot"),("comment.user.type","Bot"),("comment.user.id",8),
                ("repository.html_url",self.gl.url),("comment.body","please /review"),
                ("comment.body","/review\n--model costly"),("comment.body","> /review")):
                self.assert_ignored(client,self.gh,self.gh_payload,path,value)
            for path,value in (
                ("object_attributes.action","update"),("object_attributes.system",True),
                ("object_attributes.internal",True),("object_attributes.noteable_type","Issue"),
                ("object_attributes.note","please /review"),("user.bot",True),("user.id",8),
                ("merge_request.state","merged"),("project.web_url",self.gh.url)):
                self.assert_ignored(client,self.gl,self.gl_payload,path,value)
            import sqlite3
            with sqlite3.connect(self.state/"requests.sqlite3") as db:
                self.assertEqual(db.execute("SELECT count(*) FROM requests").fetchone(),(0,))

    def assert_ignored(self, client, project, sample, path, value):
        data = copy.deepcopy(sample)
        obj = data
        parts = path.split(".")
        for part in parts[:-1]:
            obj = obj[part]
        obj[parts[-1]] = value
        self.assertEqual(self.send(client,project,data),204,path)

    def test_both_forges_and_redelivery_after_restart(self):
        seen = []
        complete = threading.Event()
        def runner(project, number, state):
            seen.append((project.key, number))
            if len(seen) == 2:
                complete.set()
            return "reviewed"
        for iteration in range(2):
            with TestClient(make_app(self.projects, self.state, runner)) as client:
                for p,data in ((self.gh,self.gh_payload),(self.gl,self.gl_payload)):
                    self.assertEqual(self.send(client,p,data),204 if iteration else 202)
                    self.assertEqual(self.send(client,p,data),204)
                self.assertTrue(complete.wait(2))
        self.assertEqual(seen,[(self.gh.key,3),(self.gl.key,3)])

    def test_queued_survives_restart_and_started_is_never_reissued(self):
        q = Jobs(self.state)
        q.enqueue(self.gh.key,1,10)
        q.enqueue(self.gh.key,2,11)
        self.assertEqual(q.next(),(self.gh.key,1,10))
        restarted = Jobs(self.state)
        self.assertFalse(restarted.enqueue(self.gh.key,1,10))
        self.assertEqual(restarted.next(),(self.gh.key,2,11))
        self.assertIsNone(restarted.next())

    def test_wizard_and_environment(self):
        settings=dict(public_url="https://hooks.example",projects=[
            dict(provider="github",url=self.gh.url,checkout=str(self.state),requester_ids=[7],secret="wizard")])
        with patch.dict(os.environ,{},clear=True), patch("sys.stdin.isatty",return_value=True), \
             patch("builtins.input",side_effect=["https://hooks.example","github",self.gh.url,str(self.state),"7","n"]), \
             patch("cursor_pr.hook_config.getpass",return_value="wizard"):
            initial=load_config(self.state)
            self.assertEqual(load_config(self.state),initial)
            self.assertEqual((self.state/"hooks.json").stat().st_mode & 0o777,0o600)
        row=dict(settings["projects"][0],provider="gitlab",url=self.gl.url,secret_env="REVIEW_SECRET_GL")
        del row["secret"]
        with patch.dict(os.environ,dict(REVIEW_PROJECTS=json.dumps([row]),
                REVIEW_PUBLIC_URL="https://alternate.example",REVIEW_SECRET_GL="from-env")):
            url,projects=load_config(self.state)
            self.assertEqual((url,projects[self.gl.key].secret),("https://alternate.example",b"from-env"))

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
assert sys.argv[2:]==['--change','acme/app#3']
item=dict(repo='acme/app',number=3)
if kind=='change':
    item['change']=dict(url=os.environ['FIXTURE_CHANGE'],state='OPEN',headSha='a'*40,changedFiles=1)
else:
    item['changeFiles']=dict(complete=True,changedFiles=1,files=[dict(filename='demo.py',
        patchAbsent=False,status='added',patch='@@ -0,0 +1 @@\\n+x=1\\n')])
print(json.dumps(dict(schema=1,kind=kind,items=[item],partial=[])))
""")
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
assert sys.argv[1:8]==['comment','acme/app','3','--kind','mr','--head','a'*40]
assert 'REVIEW_SECRET_FIXTURE' not in os.environ
with Path(os.environ['POST_LOG']).open('a') as out:
    out.write(Path(sys.argv[-1]).read_text())
""")
        posts=self.state/"posts"
        with patch.dict(os.environ,dict(PATH=str(bindir)+os.pathsep+os.environ["PATH"],
                    POST_LOG=str(posts), REVIEW_SECRET_FIXTURE="private")):
            for project in self.projects.values():
                with patch.dict(os.environ,{"FIXTURE_CHANGE":project.change_url(3)}):
                    self.assertEqual(run_review(project,3,self.state),"reviewed")
                    self.assertEqual(run_review(project,3,self.state),"reviewed")
                    with patch.dict(os.environ,{"REVIEW_MAX_INPUT_TOKENS":"1"}):
                        self.assertEqual(run_review(project,3,self.state),"over-budget")
            with patch.dict(os.environ,{"FIXTURE_CHANGE":self.gl.change_url(3)}):
                self.assertEqual(run_review(self.gh,3,self.state),"failed")
        self.assertEqual(calls.read_text().splitlines(),["called","called"])
        self.assertEqual(posts.read_text().count("Skipped Cursor"),2)
