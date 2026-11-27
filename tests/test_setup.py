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

    def project(self, provider, again="n", producer="", requesters="13,27"):
        return [provider] + ([""] if provider == "gitlab" else []) + [f"https://{provider}.com/org/{provider}", str(self.checkout), requesters, "public", producer, again]

    def configure(self, answers, secrets=()):
        with patch("builtins.input", side_effect=[*answers, "", ""]), patch.object(setup, "hidden", side_effect=secrets):
            with patch.object(setup, "ensure_session"), patch("cursor_pr.providers.team_command", return_value={"teams": []}):
                setup.configure(self.state)

    def test_new_cli_install_defaults_to_login_and_keeps_it_on_edit(self):
        for backend in ("codex", "claude", "cursor"):
            with self.subTest(backend=backend):
                self.state = self.root / backend
                self.configure(self.project("gitlab") + ["owner:13", "", "", "", backend, "", "", "paste"],
                               ["fixture-forge"])
                config = json.loads((self.state/"setup/settings.json").read_text())
                self.assertEqual(config["settings"]["REVIEW_AUTH_MODE"], "login")
                self.assertEqual(set(config["secrets"]), {"REVIEW_GITLAB_TOKEN"})
                self.configure(["y", "", "", "", "", "", "", "", "keep"])
                edited = json.loads((self.state/"setup/settings.json").read_text())
                if backend == "cursor":
                    self.assertNotEqual(config["settings"].pop("REVIEW_LOGIN_PROFILE"),
                                        edited["settings"].pop("REVIEW_LOGIN_PROFILE"))
                self.assertEqual(edited, config)

    def test_both_forges_import_paste_and_external_mount(self):
        pem = self.root/"app.pem"
        pem.write_bytes(subprocess.check_output(["openssl","genrsa","2048"], stderr=subprocess.DEVNULL))
        external = self.root/"gitlab-pat"
        external.write_text("fixture-pat")
        self.configure(self.project("github","y","go.yml")+self.project("gitlab",producer="go-lint")+
                       ["alice:13","41","42","90","3600","7200","cursor","model-a","api-key","paste","import",str(pem),"mount",str(external)],
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
        with forge_credentials(projects):
            runtime = Path(os.environ["REVIEW_FORGE_DIR"])
            self.assertEqual((runtime/"gitlab.token").read_text().strip(),"rotated-fixture")
            self.assertEqual((runtime/"github.pem").read_bytes(),(config/"github.pem").read_bytes())

    def test_usernames_are_stored_as_numeric_ids(self):
        from cursor_pr import accounts
        pem = subprocess.check_output(["openssl", "genrsa", "2048"], stderr=subprocess.DEVNULL).decode()
        with patch.object(accounts, "github_id", return_value=13) as github:
            self.configure(self.project("github", requesters="alice,27")
                           + ["owner", "41", "42", "", "", "", "codex", "", "", "paste"], [pem])
        self.assertEqual([c.args for c in github.call_args_list], [("alice",), ("owner",)])
        config = json.loads((self.state/"setup/settings.json").read_text())
        self.assertEqual(config["projects"][0]["requester_ids"], [13, 27])
        self.assertEqual(config["settings"]["REVIEW_OPERATOR"], "owner:13")

    def test_gitlab_usernames_resolve_after_the_reviewer_token_is_pasted(self):
        from cursor_pr import accounts
        with patch.object(accounts, "gitlab_id", return_value=13) as gitlab:
            self.configure(self.project("gitlab", requesters="alice") + ["owner", "", "", "", "codex", "", "", "paste"],
                           ["fixture-forge"])
        self.assertEqual([c.args for c in gitlab.call_args_list],
                         [("alice", "https://gitlab.com", "fixture-forge"), ("owner", "https://gitlab.com", "fixture-forge")])
        config = json.loads((self.state/"setup/settings.json").read_text())
        self.assertEqual(config["projects"][0]["requester_ids"], [13])
        self.assertEqual(config["settings"]["REVIEW_OPERATOR"], "owner:13")
        setup.apply_saved_setup(self.state)
        self.assertEqual(load_config(self.state).popitem()[1].requesters, frozenset({13}))

    def test_cancel_and_reconfigure_keep_active_setup_and_env_precedence(self):
        initial = self.project("gitlab")+["owner:13","","","","","","api-key","paste","paste"]
        self.configure(initial, ["old-cursor","gl-key"])
        old = (self.state/"setup").resolve()
        with self.assertRaises(KeyboardInterrupt):
            self.configure(["y","","","","","","","api-key","paste"], [KeyboardInterrupt])
        self.assertEqual((self.state/"setup").resolve(), old)
        self.assertEqual(list(self.state.glob(".review-setup-*")), [old])
        self.configure(["y","","","5000","","","","api-key","paste","keep"], ["new-cursor"])
        self.assertFalse(old.exists())
        os.environ.update(REVIEW_GITLAB_TOKEN="from-env", REVIEW_POLL_SECONDS="45")
        setup.apply_saved_setup(self.state)
        self.assertEqual(os.environ["CURSOR_API_KEY"],"new-cursor")
        self.assertEqual(os.environ["REVIEW_GITLAB_TOKEN"],"from-env")
        self.assertEqual(os.environ["REVIEW_POLL_SECONDS"],"45")
        self.assertEqual(os.environ["REVIEW_MAX_INPUT_TOKENS"],"5000")

    def test_max_input_tokens_prefers_env_then_saved_setup_then_default(self):
        from cursor_pr import config
        self.assertEqual(config.DEFAULT_MAX_INPUT_TOKENS, 6000)
        self.assertEqual(setup.max_input_tokens(self.state), 6000)
        self.configure(self.project("gitlab")+["owner:13","","5000","","","","api-key","paste","paste"],["ck","pat"])
        self.assertEqual(setup.max_input_tokens(self.state), 5000)
        path = self.state/"setup/settings.json"
        document = json.loads(path.read_text())
        for saved, expected in (("7000", 7000), (8000, 8000), (None, 6000), ("bad", None), (0, None), ("6_000", None)):
            document["settings"]["REVIEW_MAX_INPUT_TOKENS"] = saved
            path.write_text(json.dumps(document))
            if expected is None:
                with self.assertRaisesRegex(ValueError, "REVIEW_MAX_INPUT_TOKENS must be a positive integer"):
                    setup.max_input_tokens(self.state)
            else:
                self.assertEqual(setup.max_input_tokens(self.state), expected)
        document["settings"].pop("REVIEW_MAX_INPUT_TOKENS")
        path.write_text(json.dumps(document))
        self.assertEqual(setup.max_input_tokens(self.state), 6000)
        for legacy, expected in (("9000", 9000), ("6_000", 6000), ("+6000", 6000), ("bad", None), ("-6000", None), ("6.0", None)):
            os.environ["REVIEW_MAX_INPUT_TOKENS"] = legacy
            if expected is None:
                with self.assertRaisesRegex(ValueError, "REVIEW_MAX_INPUT_TOKENS must be a positive integer"):
                    setup.max_input_tokens(self.state)
            else:
                self.assertEqual(setup.max_input_tokens(self.state), expected)

    def test_max_findings_prefers_env_then_saved_setup_then_default_for_each_tier(self):
        for name in ("REVIEW_MAX_FINDINGS", "REVIEW_MAX_FINDINGS_MID", "REVIEW_MAX_FINDINGS_MAX"):
            os.environ.pop(name, None)
        self.assertEqual(setup.max_findings(self.state), {"regular": 6, "mid": 6, "max": 6})
        self.configure(self.project("gitlab")+["owner:13","","5000","","","","api-key","paste","paste"],["ck","pat"])
        self.assertEqual(setup.max_findings(self.state), {"regular": 6, "mid": 6, "max": 6})
        path = self.state/"setup/settings.json"
        document = json.loads(path.read_text())
        document["settings"].update(REVIEW_MAX_FINDINGS="4", REVIEW_MAX_FINDINGS_MID="8", REVIEW_MAX_FINDINGS_MAX="12")
        path.write_text(json.dumps(document))
        self.assertEqual(setup.max_findings(self.state), {"regular": 4, "mid": 8, "max": 12})
        os.environ["REVIEW_MAX_FINDINGS_MID"] = "+3"
        self.assertEqual(setup.max_findings(self.state), {"regular": 4, "mid": 3, "max": 12})
        for bad in ("0", "13", "bad", "-1"):
            os.environ["REVIEW_MAX_FINDINGS_MID"] = bad
            with self.assertRaisesRegex(ValueError, "REVIEW_MAX_FINDINGS_MID must be an integer from 1 to 12"):
                setup.max_findings(self.state)
        del os.environ["REVIEW_MAX_FINDINGS_MID"]
        for bad in ("13", "06x", 0):
            document["settings"]["REVIEW_MAX_FINDINGS"] = bad
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "REVIEW_MAX_FINDINGS must be an integer from 1 to 12"):
                setup.max_findings(self.state)
        # Saved settings are exported to the environment for the poller's review children.
        document["settings"]["REVIEW_MAX_FINDINGS"] = "5"
        path.write_text(json.dumps(document))
        setup.apply_saved_setup(self.state)
        self.assertEqual(os.environ["REVIEW_MAX_FINDINGS"], "5")

    def test_saved_pat_stays_on_its_configured_host(self):
        self.configure(self.project("gitlab")+["owner:13","","","","","","api-key","paste","paste"],["ck","pat"])
        rows = json.loads((self.state/"setup/settings.json").read_text())["projects"]
        rows[0]["url"] = "https://other.example.org/org/gitlab"
        os.environ["REVIEW_PROJECTS"] = json.dumps(rows)
        with self.assertRaisesRegex(ValueError, "changing GitLab instances"):
            setup.apply_saved_setup(self.state)
        self.assertNotIn("REVIEW_GITLAB_TOKEN", os.environ)
