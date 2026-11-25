import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from dataclasses import replace
import unittest
from unittest.mock import patch

from cursor_pr import engine, history, providers, setup, sync
from cursor_pr.config import Project, project_from


class InstallTests(unittest.TestCase):
    def test_membership_selection_and_verification(self):
        teams = [{"id": i, "name": n, "isSelectable": True, "isActive": i == 1}
                 for i, n in [(1, "Personal team"), (2, "Engineering")]]
        after = [dict(t, isActive=t["id"] == 2) for t in teams]
        with patch.object(providers, "team_command", return_value={"teams": teams}) as command:
            with self.assertRaisesRegex(ValueError, "Multiple Cursor teams"):
                providers.select_team("fixture", "", Path("."))

            command.side_effect = [{"teams": teams}, {"status":"switched", "team":{"id":2}}, {"teams":after}]
            self.assertEqual(providers.select_team("fixture", "", Path("."), lambda _: "2"), "2")
            self.assertEqual(command.call_args_list[-2].args[0], ["switch", "2"])
        with patch.object(providers, "team_command", return_value={"teams": [dict(teams[0], isSelectable=False)]}):
            with self.assertRaisesRegex(ValueError, "no eligible team"):
                providers.select_team("fixture", "", Path("."))
        with patch.object(providers, "team_command", return_value={"teams": []}):
            self.assertEqual(providers.select_team("x", "", Path(".")), "")
            with self.assertRaisesRegex(ValueError, "not eligible"):
                providers.select_team("x", "5", Path("."))

    def test_claude_and_codex_single_invocations_and_separate_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = root / "calls"
            program = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
assert 'forge-canary' not in os.environ.values()
assert sys.stdin.read() == 'review fixture'
output = json.dumps({'review': {'key_issues_to_review': []}})
if name == 'codex':
    assert 'ANTHROPIC_API_KEY' not in os.environ and sys.argv[1] == 'exec'
    Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(output)
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':23}}))
else:
    assert 'CODEX_API_KEY' not in os.environ and sys.argv[1] == '-p'
    assert sys.argv[sys.argv.index('--tools')+1] == ''
    print(json.dumps(dict(type='result', subtype='success', is_error=False, result=output, usage={'input_tokens':23})))
"""
            for name in ("codex", "claude"):
                binary = root / name
                binary.write_text(program + f"with open({str(calls)!r}, 'a') as f: f.write(name+'\\n')\n")
                binary.chmod(0o755)
            with patch.dict(os.environ, {"PATH": tmp + os.pathsep + os.environ["PATH"],
                    "CODEX_API_KEY": "x", "ANTHROPIC_API_KEY": "y", "GH_TOKEN": "forge-canary"}):
                for name in ("claude", "codex"):
                    with patch.dict(os.environ, {"REVIEW_PROVIDER": name}):
                        args = ("review fixture", None, {}, root / "cache", "source")
                        first, cached = engine.review(*args)
                        self.assertFalse(cached)
                        self.assertEqual(first["cursor_usage"], {"input_tokens": 23})
                        self.assertTrue(engine.review(*args)[1])
            self.assertEqual(calls.read_text().splitlines(), ["claude", "codex"])

    def test_selected_provider_mounted_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("fixture-codex")
            with patch.dict(os.environ, {"REVIEW_PROVIDER": "codex", "CODEX_API_KEY_FILE": str(key),
                                        "CURSOR_API_KEY_FILE": "/missing-unused"}, clear=True):
                setup.apply_saved_setup(Path(tmp))
                self.assertEqual(os.environ["CODEX_API_KEY"], "fixture-codex")
                self.assertNotIn("CURSOR_API_KEY", os.environ)

    def test_browser_auth_origin_and_stale_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = subprocess.Popen(["cursor-review-container", "setup", "--web", "--state-dir", tmp],
                                     stdout=subprocess.PIPE, text=True)
            try:
                link = child.stdout.readline().strip().split()[1]
                bearer = {"Authorization": "Bearer " + link.split("#")[1]}
                def request(method="GET", headers=None, body=None):
                    conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
                    try:
                        conn.request(method, "/step", headers=headers or {}, body=body)
                        res = conn.getresponse()
                        return res.status, res.read()
                    finally:
                        conn.close()
                self.assertEqual(request()[0], 403)
                self.assertEqual(request(headers={**bearer, "Host": "other.invalid"})[0], 403)
                for _ in range(50):
                    status, body = request(headers=bearer)
                    self.assertEqual(status, 200)
                    view = json.loads(body)
                    if view["status"] == "question":
                        break
                    time.sleep(0.02)
                self.assertEqual(view["status"], "question")
                answer = json.dumps({"id": view["id"], "answer": "gitlab"})
                headers = {**bearer, "Content-Type": "application/json", "Origin": "http://other.invalid"}
                self.assertEqual(request("POST", headers, answer)[0], 403)
                headers["Origin"] = "http://127.0.0.1:8765"
                self.assertEqual(request("POST", headers, answer)[0], 200)
                self.assertEqual(request("POST", headers, answer)[0], 409)
            finally:
                child.terminate()
                child.wait(timeout=5)
                child.stdout.close()

    def test_managed_paths_and_external_checkouts(self):
        row = dict(provider="github", url="https://github.com/org/repo", requester_ids=[2], managed=True)
        project = project_from(row)
        self.assertEqual(project.checkout, Path("/repos/github/github.com/org/repo/current"))
        with self.assertRaises(ValueError):
            project_from(row | {"url": "https://github.com/../repo"})
        with patch.object(sync, "reviewer_token_file") as token:
            sync.sync_project(project_from(row | {"managed":False,"checkout":"/tmp"}), Path("/tmp"))
            token.assert_not_called()

    @unittest.skipUnless(shutil.which("git-sync"), "git-sync is included in the image")
    def test_real_sidecar_updates_default_branch_retaining_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin = base / "upstream.git"
            subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
            project = Project("gitlab", (base/"upstream").as_uri(), "org/repo",
                              base/"managed"/"current", frozenset({1}), managed=True)
            secret = base/"token"
            secret.write_text("fixture")
            with patch.object(sync, "reviewer_token_file", return_value=secret):
                for version in ("one", "two"):
                    (origin/"source").write_text(version)
                    for args in (["add", "source"], ["commit", "-qm", version]):
                        subprocess.run(["git", "-C", str(origin), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.org", *args], check=True)
                    sync.sync_project(project, base)
                    self.assertEqual((project.checkout/"source").read_text(), version)
            history.refresh(project.checkout, base / "tracking")
            self.assertEqual(history.mined_tip(base / "tracking/current"), history.tip(project.checkout))
            self.assertEqual(subprocess.check_output(["git", "-C", str(project.checkout),
                             "rev-list", "--count", "HEAD"], text=True).strip(), "2")

            # Review refs can point outside the synced default branch, including on forks.
            subprocess.run(["git", "-C", str(origin), "switch", "-qc", "candidate"], check=True)
            (origin/"source").write_text("review-only")
            subprocess.run(["git", "-C", str(origin), "-c", "user.name=Test", "-c",
                            "user.email=test@example.org", "commit", "-qam", "candidate"], check=True)
            head = history.tip(origin)
            for provider, namespace in (("github", "pull"), ("gitlab", "merge-requests")):
                subprocess.run(["git", "-C", str(origin), "update-ref", f"refs/{namespace}/8/head", head], check=True)
                with patch.object(sync, "reviewer_token_file", return_value=secret):
                    with sync.scan_checkout(project.checkout, replace(project, provider=provider), "8", head) as source:
                        self.assertEqual((source/"source").read_text(), "review-only")
                    self.assertFalse(source.exists())
            self.assertEqual((project.checkout/"source").read_text(), "two")
