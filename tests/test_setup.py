import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import setup
from cursor_pr.config import load_config
from cursor_pr.credentials import forge_credentials


class InteractiveSetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state = self.root / "state"
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        env = patch.dict(os.environ, {"PATH": os.environ["PATH"], "HOME": str(self.root/"home")}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def project(self, provider, again="n", producer=""):
        return [provider, f"https://{provider}.com/org/{provider}", str(self.checkout), "13,27", "public", producer, again]

    def configure(self, answers, secrets=()):
        with patch("builtins.input", side_effect=answers), patch.object(setup, "hidden", side_effect=secrets):
            setup.configure(self.state)

    def test_both_forges_import_paste_and_external_mount(self):
        pem = self.root/"app.pem"
        pem.write_bytes(subprocess.check_output(["openssl","genrsa","2048"], stderr=subprocess.DEVNULL))
        external = self.root/"gitlab-pat"
        external.write_text("fixture-pat")
        self.configure(self.project("github","y","go.yml")+self.project("gitlab",producer="go-lint")+
                       ["alice:13","41","42","90","3600","7200","model-a","paste","import",str(pem),"mount",str(external)],
                       ["fixture-cursor"])
        pem.unlink()  # Imported once; only the external GitLab mount is needed again.
        config = self.state/"setup"
        self.assertEqual(config.stat().st_mode & 0o777, 0o700)
        for name in ("cursor.key","github.pem","settings.json"):
            self.assertEqual((config/name).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("fixture-cursor", (config/"settings.json").read_text())
        self.assertFalse((config/"gitlab.token").exists())
        external.write_text("rotated-fixture")
        setup.apply_saved_setup(self.state)
        projects = load_config(self.state)
        self.assertEqual({p.provider: p.go_analysis for p in projects.values()},
                         {"github": "go.yml", "gitlab": "go-lint"})
        self.assertEqual(os.environ["REVIEW_POLL_SECONDS"],"90")
        self.assertEqual(os.environ["REVIEW_MAX_INPUT_TOKENS"],"3600")
        self.assertEqual(os.environ["REVIEW_QUALGEN_INTERVAL_SECONDS"],"7200")
        self.assertEqual(os.environ["REVIEW_MODEL"],"model-a")
        self.assertEqual(os.environ["CURSOR_API_KEY"],"fixture-cursor")
        with forge_credentials(projects,self.state):
            runtime = Path(os.environ["ASSAY_CONFIG_HOME"])
            self.assertEqual((runtime/"gitlab-reviewer.token").read_text().strip(),"rotated-fixture")
            self.assertEqual((runtime/"reviewer-app.pem").read_bytes(),(config/"github.pem").read_bytes())

    def test_cancel_and_reconfigure_keep_active_setup_and_env_precedence(self):
        initial = self.project("gitlab")+["owner:13","","","","","paste","paste"]
        self.configure(initial, ["old-cursor","gl-key"])
        old = (self.state/"setup").resolve()
        with self.assertRaises(KeyboardInterrupt):
            self.configure(["y","","","","","","paste"], [KeyboardInterrupt])
        self.assertEqual((self.state/"setup").resolve(), old)
        self.assertEqual(list(self.state.glob(".review-setup-*")), [old])
        self.configure(["y","","","5000","","","paste","keep"], ["new-cursor"])
        self.assertFalse(old.exists())
        os.environ.update(REVIEW_GITLAB_TOKEN="from-env", REVIEW_POLL_SECONDS="45")
        setup.apply_saved_setup(self.state)
        self.assertEqual(os.environ["CURSOR_API_KEY"],"new-cursor")
        self.assertEqual(os.environ["REVIEW_GITLAB_TOKEN"],"from-env")
        self.assertEqual(os.environ["REVIEW_POLL_SECONDS"],"45")
        self.assertEqual(os.environ["REVIEW_MAX_INPUT_TOKENS"],"5000")

    def test_saved_pat_stays_on_its_configured_host(self):
        self.configure(self.project("gitlab")+["owner:13","","","","","paste","paste"],["ck","pat"])
        rows = json.loads((self.state/"setup/settings.json").read_text())["projects"]
        rows[0]["url"] = "https://other.example.org/org/gitlab"
        os.environ["REVIEW_PROJECTS"] = json.dumps(rows)
        with self.assertRaisesRegex(ValueError, "changing GitLab instances"):
            setup.apply_saved_setup(self.state)
        self.assertNotIn("REVIEW_GITLAB_TOKEN", os.environ)
