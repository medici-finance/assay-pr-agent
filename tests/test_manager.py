import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cursor_pr import provision
from cursor_pr.config import project_from
from cursor_pr.manager import ConfigurationWatch
from cursor_pr.web_setup import Session


def project(provider, slug):
    base = "https://git.example:8443/code" if provider == "gitlab" else "https://github.com"
    return project_from(dict(provider=provider, gitlab_base=base, url=base + "/" + slug,
                             managed=True, requester_ids=[1]))


class ManagerTests(unittest.TestCase):
    def test_manifest_callback_is_single_use_and_bound_to_registration(self):
        session = Session()
        nonce, form = provision.manifest("acme", False)
        self.assertEqual(form["manifest"]["hook_attributes"],
                         {"url": "https://github.com/medici-finance/assay-pr-agent", "active": False})
        self.assertIn("/organizations/acme/settings/apps/new", form["action"])
        result = []
        worker = threading.Thread(target=lambda: result.append(session.register(nonce, form)), daemon=True)
        worker.start()
        with session.cv:
            self.assertTrue(session.cv.wait_for(lambda: session.nonce is not None, timeout=2))
        self.assertFalse(session.submit({"id": session.view["id"], "answer": "invalid"}))
        self.assertFalse(session.callback({"state": ["wrong"], "code": ["one"]}))
        self.assertFalse(session.callback({"state": [nonce], "code": ["../one"]}))
        callback = {"state": [nonce], "code": ["one"]}
        self.assertTrue(session.callback(callback))
        self.assertFalse(session.callback(callback))
        worker.join(timeout=2)
        self.assertEqual(result, ["one"])

    def test_app_exchange_then_resumable_verified_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            pem = subprocess.check_output(["openssl", "genrsa", "2048"], stderr=subprocess.DEVNULL).decode()
            session = Mock()
            session.choose.return_value = "organization"
            session.register.return_value = "fixture"
            repos = [project("github", "org/a"), project("github", "org/b")]
            with patch.object(provision, "request", side_effect=[dict(id=7, slug="example", pem=pem,
                    client_secret="discard-this"), {"id": 22}, {"id": 22}]) as api:
                settings, pending = provision.github(session, state, repos, state/"app.pem")
            self.assertEqual([c.args[0] for c in api.call_args_list], ["POST", "GET", "GET"])
            self.assertEqual(settings[provision.INSTALL_ID], "22")
            self.assertNotIn("discard-this", pending.read_text())
            self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
            with patch.object(provision, "request", return_value={"id": 22}) as api:
                provision.github(session, state, repos, state/"app.pem")
            self.assertEqual([c.args[0] for c in api.call_args_list], ["GET", "GET"])

    def test_gitlab_provisioning_scopes_host_and_preserves_only_service_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            session = Mock()
            session.ask.return_value = "bootstrap-only"
            repos = [project("gitlab", "org/a"), project("gitlab", "org/b")]
            for selected, endpoint in [(repos[:1], "projects/org%2Fa"), (repos, "groups/org")]:
                with patch.object(provision, "request", return_value={"token": "service-token"}) as api:
                    result, pending = provision.gitlab(session, state, selected)
                    self.assertEqual(result, "service-token")
                    self.assertEqual(api.call_args.args[1], "https://git.example:8443/code/api/v4/" + endpoint + "/access_tokens")
                    self.assertEqual(api.call_args.kwargs["json"]["access_level"], 20)
                    self.assertNotIn("bootstrap-only", pending.read_text())
                    provision.gitlab(session, state, selected)
                    api.assert_called_once()
            with self.assertRaisesRegex(ValueError, "one GitLab group"):
                provision.gitlab(session, state, [*repos, project("gitlab", "another/project")])

    def test_configuration_change_wakes_workers(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"REVIEW_WATCH_SETUP": "1"}):
            state = Path(tmp)
            link = state/"setup"
            link.symlink_to("old")
            watch = ConfigurationWatch(state)
            new = state/"new"
            new.symlink_to("updated")
            new.replace(link)
            self.assertTrue(watch.changed())
            watch.wait(120)
