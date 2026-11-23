import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr.config import project_from
from cursor_pr.credentials import KEY, TOKEN, forge_credentials
from cursor_pr.source import run_desk


class ContainerCredentialsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.root / "home"), "PATH": os.environ["PATH"],
                                          "REVIEW_OPERATOR": "owner:17"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.projects = {p: project_from(dict(provider=p, url=f"https://{p}.com/example/{p}",
                        checkout=str(self.root), requester_ids=[17], visibility="public"))
                         for p in ("github", "gitlab")}

    def test_both_provider_secrets_and_real_assay_custody(self):
        # Generate a disposable RSA key. Exercise the one-line Docker env-file format.
        pem = subprocess.check_output(["openssl","genrsa","2048"], stderr=subprocess.DEVNULL).decode()
        mounted = self.root / "mounted-token"
        mounted.write_text("fixture-pat\n")
        mounted.chmod(0o400)
        os.environ.update(REVIEW_GITHUB_PRIVATE_KEY=pem.strip().replace("\n","\\n"),
                          REVIEW_GITHUB_APP_ID="101", REVIEW_GITHUB_INSTALLATION_ID="202",
                          REVIEW_GITLAB_TOKEN_FILE=str(mounted))
        with forge_credentials(self.projects, self.root):
            runtime = Path(os.environ["ASSAY_CONFIG_HOME"])
            self.assertEqual((runtime/"reviewer-app.pem").read_text().strip(), pem.strip())
            for name in ("reviewer-app.pem","gitlab-reviewer.token"):
                self.assertEqual((runtime/name).stat().st_mode & 0o777, 0o600)
            self.assertNotIn(KEY, os.environ)
            self.assertNotIn(TOKEN+"_FILE", os.environ)
            self.assertEqual(os.environ["GITLAB_API_BASE"], "https://gitlab.com/api/v4")
            self.assertEqual(os.environ["REVIEWER_INSTALL_ID"], "202")
            roster = Path(os.environ["REVIEW_ASSAY_HOME"])/".config/assay/roster.env"
            self.assertIn("example/gitlab=gitlab", roster.read_text())
            self.assertIn("example/github:public", roster.read_text())
            self.assertNotIn("fixture-pat", roster.read_text())
            self.assertEqual(roster.stat().st_mode & 0o777, 0o600)
            if shutil.which("desktoken"):
                result = run_desk(["desktoken","--no-rotate","--forge","gitlab","reviewer",
                                   "--repo","example/gitlab"], cwd=self.root)
                self.assertEqual(Path(result.decode().strip()), runtime/"gitlab-reviewer.token")
        self.assertFalse(runtime.exists())
        self.assertEqual(mounted.read_text(), "fixture-pat\n")
        self.assertFalse((self.root/"home/.config/assay").exists())

    def test_gitlab_only_literal_value_and_mounted_roster(self):
        os.environ.pop("REVIEW_OPERATOR")
        os.environ[TOKEN] = "fixture-pat"
        roster = Path.home()/".config/assay/roster.env"
        roster.parent.mkdir(parents=True)
        roster.write_text("existing settings")
        with forge_credentials({"gl":self.projects["gitlab"]}, self.root):
            self.assertEqual((Path(os.environ["ASSAY_CONFIG_HOME"])/"gitlab-reviewer.token").read_text(),
                             "fixture-pat\n")
            self.assertNotIn("REVIEW_ASSAY_HOME", os.environ)
        self.assertEqual(roster.read_text(), "existing settings")

    def test_rejects_incomplete_or_ambiguous_setup_without_echoing_secrets(self):
        cases = [
            ("gitlab", {TOKEN:"sensitive", TOKEN+"_FILE":"/secret"}, "only one"),
            ("gitlab", {TOKEN:"sensitive", "GITLAB_API_BASE":"https://other.test/api/v4"}, "must match"),
            ("gitlab", {TOKEN:"sensitive", "REVIEW_OPERATOR":"owner:1\nother=value"}, "responsible human"),
            ("gitlab", {TOKEN+"_FILE":"relative"}, "absolute path"),
            ("github", {"REVIEW_GITHUB_APP_ID":"1", "REVIEW_GITHUB_INSTALLATION_ID":"2",
                        KEY:"sensitive"}, "RSA PEM"),
        ]
        for provider, environment, error in cases:
            with self.subTest(provider=provider, error=error), patch.dict(os.environ, environment):
                with self.assertRaisesRegex(ValueError,error) as caught:
                    with forge_credentials({"project":self.projects[provider]}, self.root):
                        self.fail("Invalid configuration was accepted")
                self.assertNotIn("sensitive", str(caught.exception))
