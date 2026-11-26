import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import accounts, providers, settings, setup
from cursor_pr.manager import ConfigurationWatch
from cursor_pr.web_setup import Session

SAVED = {"REVIEW_OPERATOR": "alice:13", "REVIEW_GITHUB_APP_ID": "41", "REVIEW_GITHUB_INSTALLATION_ID": "42",
         "REVIEW_MODEL": "model-a", "REVIEW_MAX_INPUT_TOKENS": "6000", "REVIEW_POLL_SECONDS": "120",
         "REVIEW_QUALGEN_INTERVAL_SECONDS": "86400", "REVIEW_PROVIDER": "codex", "REVIEW_AUTH_MODE": "api-key"}
# A self-signed fixture CA and its empty CRL, which TLS can load.
CERTIFICATE = ("-----BEGIN CERTIFICATE-----\n"
    "MIIBYDCCAQagAwIBAgIUBVyXEMpP575+xujoKHFlvoca0vQwCgYIKoZIzj0EAwIw\n"
    "FTETMBEGA1UEAwwKZml4dHVyZS1jYTAgFw0yNjExMjYyMzU2NDNaGA8yMTI2MTEw\n"
    "MjIzNTY0M1owFTETMBEGA1UEAwwKZml4dHVyZS1jYTBZMBMGByqGSM49AgEGCCqG\n"
    "SM49AwEHA0IABEz5I50H+TlUcUvAxZGhn0kSYp8eOomNbQkV7upHhUHgyX4Es+o+\n"
    "8EXYmJWPEEXNgoGw7Oh2or3KnV/KbODi5SyjMjAwMB0GA1UdDgQWBBTC6r8bfEDE\n"
    "cwPCPKO5Gz+drZXQJjAPBgNVHRMBAf8EBTADAQH/MAoGCCqGSM49BAMCA0gAMEUC\n"
    "IQDz6m9hNAXSJ9OB2wfgNmPsReCVdlyFKciRY8CkoTJKHQIgFhydjU3pnAT3LvZ0\n"
    "o8RtYf/vuOMVzJu6sWFF4oQ3GeY=\n"
    "-----END CERTIFICATE-----")
REVOCATIONS = ("-----BEGIN X509 CRL-----\n"
    "MIGuMFYCAQEwCgYIKoZIzj0EAwIwFTETMBEGA1UEAwwKZml4dHVyZS1jYRcNMjYx\n"
    "MTI2MjM1NjQzWhgPMjEyNjExMDIyMzU2NDNaoA4wDDAKBgNVHRQEAwIBATAKBggq\n"
    "hkjOPQQDAgNIADBFAiEAsU5UF7oZ9q3fU0YjQt+vkzN5hER/m0ThEU+fDecGoL0C\n"
    "IBfTO5S47Ioiazhf3x4kzpwnJNXAMb/tksERuLXPKDAc\n"
    "-----END X509 CRL-----")
# Matches the PEM pattern but is not a certificate.
NOT_A_CERTIFICATE = "-----BEGIN CERTIFICATE-----\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n-----END CERTIFICATE-----"


def saved(state, checkout, *, values=SAVED, projects=None, secrets=None):
    """A saved setup laid out as the wizard leaves it; an absolute secret value is a mounted file."""
    projects = projects or [dict(provider="github", url="https://github.com/org/repo", checkout=str(checkout),
                                 requester_ids=[13, 27], visibility="public")]
    secrets = secrets or {"CODEX_API_KEY": "fixture-codex", setup.KEY: "fixture-pem"}
    bundle = state / ".review-setup-1"
    bundle.mkdir(mode=0o700)
    for name, value in secrets.items():
        if not value.startswith("/"):
            path = bundle / setup.SECRETS[name]
            path.write_text(value + "\n")
            path.chmod(0o600)
    document = {"settings": values, "projects": projects, "secrets": {
        name: value if value.startswith("/") else setup.SECRETS[name] for name, value in secrets.items()}}
    (bundle / "settings.json").write_text(json.dumps(document))
    (bundle / "settings.json").chmod(0o600)
    (state / "setup").symlink_to(bundle.name)


def item(view, name):
    return next(row for section in view["sections"] for row in section["settings"] if row["name"] == name)


def wizard(state, answers, secrets):
    with patch("builtins.input", side_effect=[*answers, "", ""]), patch.object(setup, "hidden", side_effect=secrets):
        with patch.object(setup, "ensure_session"), patch("cursor_pr.providers.team_command", return_value={"teams": []}):
            setup.configure(state)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        env = patch.dict(os.environ, {"PATH": os.environ["PATH"], "HOME": str(self.root/"home")}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def seed(self, **changes):
        shutil.rmtree(self.state)
        self.state.mkdir()
        saved(self.state, self.checkout, **changes)

    def document(self):
        return json.loads((setup.active_bundle(self.state) / "settings.json").read_text())

    def test_one_setting_changes_and_everything_else_is_kept(self):
        self.seed()
        before, old = self.document(), setup.active_bundle(self.state)
        pem = (old / "github.pem").read_bytes()
        with patch.dict(os.environ, {"REVIEW_WATCH_SETUP": "1"}):
            watch = ConfigurationWatch(self.state)
            settings.save(self.state, {"name": "REVIEW_MAX_INPUT_TOKENS", "value": " 8000 "})
            self.assertTrue(watch.changed())
        after = self.document()
        self.assertEqual(after["settings"].pop("REVIEW_MAX_INPUT_TOKENS"), "8000")
        before["settings"].pop("REVIEW_MAX_INPUT_TOKENS")
        self.assertEqual(after, before)
        new = setup.active_bundle(self.state)
        self.assertEqual(list(self.state.glob(".review-setup-*")), [new])
        self.assertEqual(new.stat().st_mode & 0o777, 0o700)
        for name in ("codex.key", "github.pem", "settings.json"):
            self.assertEqual((new / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual((new / "github.pem").read_bytes(), pem)
        setup.apply_saved_setup(self.state)
        self.assertEqual(os.environ["REVIEW_MAX_INPUT_TOKENS"], "8000")
        self.assertEqual(os.environ["CODEX_API_KEY"], "fixture-codex")

    def test_invalid_values_keep_the_active_setup(self):
        self.seed()
        link = (self.state / "setup").readlink()
        document = (setup.active_bundle(self.state) / "settings.json").read_bytes()
        for change in ({"name": "REVIEW_MAX_INPUT_TOKENS", "value": "0"}, {"name": "REVIEW_MAX_INPUT_TOKENS", "value": "many"},
                       {"name": "REVIEW_MAX_INPUT_TOKENS", "value": "6_000"}, {"name": "REVIEW_POLL_SECONDS", "value": "-5"},
                       {"name": "REVIEW_QUALGEN_INTERVAL_SECONDS", "value": "-1"}, {"name": "REVIEW_OSV_DOWNLOAD_DATABASES", "value": "yes"},
                       {"name": "REVIEW_GITHUB_APP_ID", "value": "forty-one"},
                       {"name": "REVIEW_PROVIDER", "value": "cursor"}, {"name": "REVIEW_AUTH_MODE", "value": "login"},
                       {"name": "REVIEW_LOGIN_PROFILE", "value": "x"}, {"name": "REVIEW_GITLAB_TOKEN", "value": "x"},
                       {"name": "REVIEW_CURSOR_TEAM_ID", "value": "2"}, {"name": "nope", "value": "x"}, {"name": None, "value": "x"},
                       {"name": "REVIEW_MAX_INPUT_TOKENS", "value": 8000}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                settings.save(self.state, change)
        self.assertEqual((self.state / "setup").readlink(), link)
        self.assertEqual((setup.active_bundle(self.state) / "settings.json").read_bytes(), document)
        self.assertEqual(len(list(self.state.glob(".review-setup-*"))), 1)

    def test_another_installer_blocks_the_save(self):
        self.seed()
        with setup.installer_lock(self.state):
            with self.assertRaisesRegex(ValueError, "Another installer is active"):
                settings.save(self.state, {"name": "REVIEW_POLL_SECONDS", "value": "60"})

    def test_settings_from_the_container_environment_are_read_only(self):
        self.seed()
        with patch.dict(os.environ, {"REVIEW_MAX_INPUT_TOKENS": "7000", "CODEX_API_KEY": "from-env"}):
            row = item(settings.view(self.state), "REVIEW_MAX_INPUT_TOKENS")
            self.assertEqual((row["locked"], row["source"], row["value"]), (True, "env", "7000"))
            with self.assertRaisesRegex(ValueError, "container environment"):
                settings.save(self.state, {"name": "REVIEW_MAX_INPUT_TOKENS", "value": "8000"})
            with self.assertRaisesRegex(ValueError, "container environment"):
                settings.save(self.state, {"name": "CODEX_API_KEY", "value": "rotated"})
            self.assertEqual(item(settings.view(self.state), "CODEX_API_KEY")["state"], "env")
        with patch.dict(os.environ, {"REVIEW_PROJECTS": "[]"}):
            rows = [row for project in settings.view(self.state)["projects"] for row in project["settings"]]
            self.assertEqual({row["name"] for row in rows}, set(settings.PROJECT_FIELDS))
            self.assertEqual({row["locked"] for row in rows}, {True})
            with self.assertRaisesRegex(ValueError, "container environment"):
                settings.save(self.state, {"project": "https://github.com/org/repo", "name": "visibility", "value": "internal"})

    def test_values_show_where_they_come_from(self):
        self.seed(values={k: v for k, v in SAVED.items() if k != "REVIEW_MODEL"})
        view = settings.view(self.state)
        self.assertEqual((item(view, "REVIEW_SYNC_SECONDS")["source"], item(view, "REVIEW_SYNC_SECONDS")["value"]), ("default", "120"))
        self.assertEqual((item(view, "REVIEW_MODEL")["source"], item(view, "REVIEW_MODEL")["value"]), ("default", "auto"))
        self.assertEqual((item(view, "REVIEW_MAX_INPUT_TOKENS")["source"], item(view, "REVIEW_MAX_INPUT_TOKENS")["value"]), ("saved", "6000"))
        self.assertNotIn("REVIEW_CURSOR_TEAM_ID", [row["name"] for section in view["sections"] for row in section["settings"]])
        self.assertEqual([item(view, name)["value"] for name in ("REVIEW_PROVIDER", "REVIEW_AUTH_MODE")], ["Codex", "API key"])
        self.assertTrue(all(item(view, name)["wizard"] for name in ("REVIEW_PROVIDER", "REVIEW_AUTH_MODE")))
        settings.save(self.state, {"name": "REVIEW_MODEL", "value": "model-b"})
        self.assertEqual(self.document()["settings"]["REVIEW_MODEL"], "model-b")
        settings.save(self.state, {"name": "REVIEW_MODEL", "value": "auto"})
        self.assertNotIn("REVIEW_MODEL", self.document()["settings"])
        settings.save(self.state, {"name": "REVIEW_OSV_DOWNLOAD_DATABASES", "value": "0"})
        settings.save(self.state, {"name": "REVIEW_QUALGEN_INTERVAL_SECONDS", "value": "0"})
        self.assertEqual({k: self.document()["settings"][k] for k in ("REVIEW_OSV_DOWNLOAD_DATABASES", "REVIEW_QUALGEN_INTERVAL_SECONDS")},
                         {"REVIEW_OSV_DOWNLOAD_DATABASES": "0", "REVIEW_QUALGEN_INTERVAL_SECONDS": "0"})

    def test_cursor_team_is_checked_without_switching(self):
        teams = [{"id": i, "name": n, "isSelectable": True, "isActive": i == 1} for i, n in [(1, "Personal team"), (2, "Engineering")]]
        self.seed(values={**SAVED, "REVIEW_PROVIDER": "cursor"}, secrets={"CURSOR_API_KEY": "fixture-cursor"})
        with patch.object(providers, "team_command", return_value={"teams": teams}) as command:
            self.assertEqual(item(settings.view(self.state), "REVIEW_CURSOR_TEAM_ID")["value"], "")
            settings.save(self.state, {"name": "REVIEW_CURSOR_TEAM_ID", "value": "2"})
            self.assertEqual([c.args[0] for c in command.call_args_list], [["list"]])
            self.assertEqual(command.call_args.args[:2], (["list"], "fixture-cursor"))
            self.assertEqual(self.document()["settings"]["REVIEW_CURSOR_TEAM_ID"], "2")
            with self.assertRaisesRegex(ValueError, "not eligible"):
                settings.save(self.state, {"name": "REVIEW_CURSOR_TEAM_ID", "value": "5"})
            with self.assertRaisesRegex(ValueError, "Multiple Cursor teams"):
                settings.save(self.state, {"name": "REVIEW_CURSOR_TEAM_ID", "value": ""})
        profile = "a" * 32
        self.seed(values={**SAVED, "REVIEW_PROVIDER": "cursor", "REVIEW_AUTH_MODE": "login", "REVIEW_LOGIN_PROFILE": profile})
        with patch.object(providers, "team_command", return_value={"teams": teams}) as command:
            settings.save(self.state, {"name": "REVIEW_CURSOR_TEAM_ID", "value": "1"})
            self.assertEqual([c.args[0] for c in command.call_args_list], [["list"]])
            # The live sign-in profile is listed, never switched.
            self.assertTrue(command.call_args.args[2].is_relative_to(self.state / "provider-login"))
        self.assertEqual(self.document()["settings"]["REVIEW_LOGIN_PROFILE"], profile)
        self.assertEqual(self.document()["settings"]["REVIEW_CURSOR_TEAM_ID"], "1")

    def test_usernames_resolve_for_the_operator_and_requesters(self):
        self.seed()
        url = "https://github.com/org/repo"
        with patch.object(accounts, "github_id", return_value=13) as github:
            settings.save(self.state, {"name": "REVIEW_OPERATOR", "value": "bob"})
            settings.save(self.state, {"project": url, "name": "requester_ids", "value": "carol, 27,"})
        self.assertEqual([c.args for c in github.call_args_list], [("bob",), ("carol",)])
        document = self.document()
        self.assertEqual(document["settings"]["REVIEW_OPERATOR"], "bob:13")
        self.assertEqual(document["projects"][0]["requester_ids"], [13, 27])
        self.assertEqual(settings.view(self.state)["projects"][0]["settings"][0]["value"], "13, 27")

    def test_operator_and_requesters_follow_the_wizard_rules_for_two_forges(self):
        gitlab = dict(provider="gitlab", url="https://gitlab.com/org/other", checkout=str(self.checkout),
                      requester_ids=[13], visibility="private")
        github = dict(provider="github", url="https://github.com/org/repo", checkout=str(self.checkout), requester_ids=[13])
        self.seed(projects=[github, gitlab], secrets={"CODEX_API_KEY": "fixture-codex", setup.KEY: "fixture-pem", "REVIEW_GITLAB_TOKEN": "fixture-pat"})
        with self.assertRaisesRegex(ValueError, "username:numeric-id"):
            settings.save(self.state, {"name": "REVIEW_OPERATOR", "value": "bob"})
        settings.save(self.state, {"name": "REVIEW_OPERATOR", "value": "bob:13"})
        self.assertEqual(self.document()["settings"]["REVIEW_OPERATOR"], "bob:13")
        with patch.object(accounts, "gitlab_id", return_value=13) as lookup:
            settings.save(self.state, {"project": gitlab["url"], "name": "requester_ids", "value": "dana"})
        self.assertEqual(lookup.call_args.args, ("dana", "https://gitlab.com", "fixture-pat"))
        self.seed(projects=[gitlab], secrets={"CODEX_API_KEY": "fixture-codex"})
        with self.assertRaisesRegex(ValueError, "need the reviewer token"):
            settings.save(self.state, {"project": gitlab["url"], "name": "requester_ids", "value": "dana"})
        # A bare GitLab username cannot be resolved without the reviewer token.
        with self.assertRaisesRegex(ValueError, "login:numeric-id"):
            settings.save(self.state, {"name": "REVIEW_OPERATOR", "value": "bob"})

    def test_credential_replacement_validates_and_never_shows_values(self):
        genrsa = lambda: subprocess.check_output(["openssl", "genrsa", "2048"], stderr=subprocess.DEVNULL).decode()
        pem = genrsa()
        self.seed(secrets={"CODEX_API_KEY": "fixture-codex", setup.KEY: pem})
        view = json.dumps(settings.view(self.state))
        self.assertNotIn("fixture-codex", view)
        self.assertNotIn("PRIVATE KEY", view)
        self.assertEqual([item(json.loads(view), name)["state"] for name in (setup.KEY, "CODEX_API_KEY")], ["stored", "stored"])
        replacement = genrsa()
        settings.save(self.state, {"name": setup.KEY, "value": replacement.replace("\n", "\\n")})
        new = setup.active_bundle(self.state)
        self.assertEqual((new / "github.pem").read_text(), replacement.rstrip() + "\n")
        self.assertEqual((new / "codex.key").read_text(), "fixture-codex\n")
        settings.save(self.state, {"name": "CODEX_API_KEY", "value": "rotated-codex"})
        self.assertEqual((setup.active_bundle(self.state) / "codex.key").read_text(), "rotated-codex\n")
        self.assertNotIn("rotated-codex", json.dumps(settings.view(self.state)))
        before = (setup.active_bundle(self.state) / "settings.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "RSA PEM"):
            settings.save(self.state, {"name": setup.KEY, "value": "not a pem"})
        with self.assertRaisesRegex(ValueError, "printable token"):
            settings.save(self.state, {"name": "CODEX_API_KEY", "value": "has a space"})
        self.assertEqual((setup.active_bundle(self.state) / "settings.json").read_bytes(), before)

    def test_optional_gitlab_tls_files_can_be_replaced_and_removed(self):
        gitlab = dict(provider="gitlab", url="https://gitlab.com/org/repo", checkout=str(self.checkout), requester_ids=[13])
        self.seed(projects=[gitlab], secrets={"REVIEW_GITLAB_TOKEN": "fixture-pat", "REVIEW_GITLAB_CA": CERTIFICATE})
        view = settings.view(self.state)
        self.assertEqual([item(view, name)["state"] for name in ("REVIEW_GITLAB_TOKEN", setup.gitlab_tls.CA, setup.gitlab_tls.CRL)],
                         ["stored", "stored", "none"])
        settings.save(self.state, {"name": setup.gitlab_tls.CRL, "value": REVOCATIONS})
        self.assertIn(setup.gitlab_tls.CRL, self.document()["secrets"])
        settings.save(self.state, {"name": setup.gitlab_tls.CA, "remove": True})
        self.assertNotIn(setup.gitlab_tls.CA, self.document()["secrets"])
        self.assertFalse((setup.active_bundle(self.state) / "gitlab-ca.pem").exists())
        self.assertTrue((setup.active_bundle(self.state) / "gitlab-crl.pem").is_file())
        with self.assertRaisesRegex(ValueError, "cannot be removed"):
            settings.save(self.state, {"name": "REVIEW_GITLAB_TOKEN", "remove": True})
        with self.assertRaisesRegex(ValueError, "CERTIFICATE data"):
            settings.save(self.state, {"name": setup.gitlab_tls.CA, "value": "not a certificate"})
        before = (setup.active_bundle(self.state) / "settings.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "could not be loaded for TLS"):
            settings.save(self.state, {"name": setup.gitlab_tls.CA, "value": NOT_A_CERTIFICATE})
        self.assertEqual((setup.active_bundle(self.state) / "settings.json").read_bytes(), before)
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.startswith(".review-setup-")],
                         [setup.active_bundle(self.state).name])

    def test_a_mounted_credential_stays_a_reference_until_replaced(self):
        external = self.root / "gitlab-pat"
        external.write_text("fixture-pat")
        gitlab = dict(provider="gitlab", url="https://gitlab.com/org/repo", checkout=str(self.checkout), requester_ids=[13])
        self.seed(projects=[gitlab], secrets={"REVIEW_GITLAB_TOKEN": str(external)})
        settings.save(self.state, {"name": "REVIEW_POLL_SECONDS", "value": "60"})
        self.assertEqual(self.document()["secrets"], {"REVIEW_GITLAB_TOKEN": str(external)})
        self.assertEqual([p.name for p in setup.active_bundle(self.state).iterdir()], ["settings.json"])
        self.assertEqual((item(settings.view(self.state), "REVIEW_GITLAB_TOKEN")["state"], item(settings.view(self.state), "REVIEW_GITLAB_TOKEN")["value"]),
                         ("mounted", str(external)))
        settings.save(self.state, {"name": "REVIEW_GITLAB_TOKEN", "value": "new-pat"})
        self.assertEqual(self.document()["secrets"], {"REVIEW_GITLAB_TOKEN": "gitlab.token"})
        self.assertEqual((setup.active_bundle(self.state) / "gitlab.token").read_text(), "new-pat\n")

    def test_project_fields_validate_like_the_wizard(self):
        self.seed()
        url = "https://github.com/org/repo"
        settings.save(self.state, {"project": url, "name": "visibility", "value": "internal"})
        settings.save(self.state, {"project": url, "name": "go_analysis", "value": "go-lint.yml"})
        settings.save(self.state, {"project": url, "name": "scanner_ci", "value": '{"hadolint": {"producer": "lint.yml", "all_paths": true}}'})
        row = self.document()["projects"][0]
        self.assertEqual((row["visibility"], row["go_analysis"], row["scanner_ci"]),
                         ("internal", "go-lint.yml", {"hadolint": {"producer": "lint.yml", "all_paths": True}}))
        settings.save(self.state, {"project": url, "name": "go_analysis", "value": " "})
        settings.save(self.state, {"project": url, "name": "scanner_ci", "value": "{}"})
        row = self.document()["projects"][0]
        self.assertNotIn("go_analysis", row)
        self.assertNotIn("scanner_ci", row)
        before = (setup.active_bundle(self.state) / "settings.json").read_bytes()
        for field, value, message in (("visibility", "hidden", "visibility must be"), ("go_analysis", "not a workflow!", "GitHub workflow filename"),
                                      ("scanner_ci", "{nope", "JSON object"), ("scanner_ci", "[]", "JSON object"),
                                      ("scanner_ci", '{"nope": {}}', "scanner_ci must map"), ("requester_ids", "", "requester_ids must list"),
                                      ("requester_ids", "13,,!", "forge usernames"), ("checkout", "/tmp", "Unknown project setting")):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, message):
                settings.save(self.state, {"project": url, "name": field, "value": value})
            self.assertEqual((setup.active_bundle(self.state) / "settings.json").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "Unknown repository"):
            settings.save(self.state, {"project": "https://github.com/org/other", "name": "visibility", "value": "internal"})

    def test_a_wizard_rerun_keeps_what_only_the_settings_page_edits(self):
        wizard(self.state, ["gitlab", "", "https://gitlab.com/org/repo", str(self.checkout), "13,27", "public", "", "n",
                            "owner:13", "", "", "", "codex", "", "", "paste"], ["fixture-forge"])
        settings.save(self.state, {"name": "REVIEW_SYNC_SECONDS", "value": "300"})
        settings.save(self.state, {"name": "REVIEW_OSV_DOWNLOAD_DATABASES", "value": "0"})
        setup.apply_saved_setup(self.state)
        self.assertEqual((os.environ["REVIEW_SYNC_SECONDS"], os.environ["REVIEW_OSV_DOWNLOAD_DATABASES"]), ("300", "0"))
        wizard(self.state, ["y", "", "", "", "", "", "", "", "keep"], [])
        kept = self.document()["settings"]
        self.assertEqual((kept["REVIEW_SYNC_SECONDS"], kept["REVIEW_OSV_DOWNLOAD_DATABASES"]), ("300", "0"))


class SettingsSessionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        saved(self.state, self.checkout)

    def settled(self, session, status):
        with session.cv:
            self.assertTrue(session.cv.wait_for(lambda: session.view["status"] == status, timeout=5), session.view)

    def test_a_wizard_started_from_the_settings_page_returns_to_it(self):
        session = Session()
        session.show(settings.view(self.state))
        with patch.object(setup, "configure") as configure:
            session.launch(self.state, resume=True)
            self.settled(session, "settings")
            configure.assert_called_once_with(self.state, force_login=False)
            self.assertEqual(session.view["messages"], ["The setup wizard saved the configuration."])
            configure.side_effect = ValueError("GitHub has no user account named 'bob'")
            session.launch(self.state, resume=True)
            self.settled(session, "settings")
            self.assertEqual(session.view["messages"], ["GitHub has no user account named 'bob'"])
            # A first install or --reauth run still ends the session when it finishes.
            configure.side_effect = None
            session.launch(self.state)
            self.settled(session, "complete")

    def test_closing_the_settings_page_ends_the_session(self):
        session = Session()
        session.show(settings.view(self.state))
        current = session.view["id"]
        self.assertFalse(session.quit(current - 1))
        self.assertFalse(session.submit({"id": current, "answer": "x"}))
        self.assertEqual(session.view["status"], "settings")
        self.assertTrue(session.quit(current))
        self.assertEqual(session.view["status"], "quit")
        self.assertIsNone(session.answer)
        # A save that finishes after a quit from the terminal does not reopen the page.
        self.assertIsNone(session.show(settings.view(self.state), "Saved."))
        self.assertEqual(session.view["status"], "quit")
