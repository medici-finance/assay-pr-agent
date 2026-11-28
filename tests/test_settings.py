import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from cursor_pr import accounts, dialog, providers, settings, setup, web_setup
from cursor_pr.manager import ConfigurationWatch
from cursor_pr.web_setup import Session, change_setting

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

    def test_an_existing_relative_credential_path_survives_an_unrelated_save(self):
        self.seed()
        old = setup.active_bundle(self.state)
        location = "keys/provider.key"
        (old / "keys").mkdir()
        (old / "codex.key").rename(old / location)
        document = self.document()
        document["secrets"]["CODEX_API_KEY"] = location
        (old / "settings.json").write_text(json.dumps(document))
        with patch.dict(os.environ):
            setup.apply_saved_setup(self.state)
            self.assertEqual(os.environ["CODEX_API_KEY"], "fixture-codex")
        settings.save(self.state, {"name": "REVIEW_MAX_INPUT_TOKENS", "value": "8000"})
        setup.apply_saved_setup(self.state)
        self.assertEqual(os.environ["CODEX_API_KEY"], "fixture-codex")

    def test_an_absolute_credential_path_through_the_old_bundle_survives_a_save(self):
        for reference in ("active symlink", "bundle directory", "external symlink", "symlink chain"):
            with self.subTest(reference=reference):
                self.seed()
                old = setup.active_bundle(self.state)
                path = old / "codex.key"
                (old / "codex.key").chmod(0o644)
                if reference == "active symlink":
                    path = self.state / "setup" / "codex.key"
                elif reference == "external symlink":
                    path = self.root / "credential-alias"
                    path.symlink_to(old / "codex.key")
                elif reference == "symlink chain":
                    external = self.root / "actual-key"
                    path.rename(external)
                    path.symlink_to(external)
                    alias = self.root / "chained-alias"
                    alias.symlink_to(self.state / "setup" / "codex.key")
                    path = alias
                document = self.document()
                document["secrets"]["CODEX_API_KEY"] = str(path)
                (old / "settings.json").write_text(json.dumps(document))
                with patch.dict(os.environ):
                    setup.apply_saved_setup(self.state)
                    self.assertEqual(os.environ["CODEX_API_KEY"], "fixture-codex")
                settings.save(self.state, {"name": "REVIEW_MAX_INPUT_TOKENS", "value": "8000"})
                with patch.dict(os.environ):
                    setup.apply_saved_setup(self.state)
                    self.assertEqual(os.environ["CODEX_API_KEY"], "fixture-codex")
                self.assertEqual(self.document()["secrets"]["CODEX_API_KEY"], "codex.key")
                self.assertEqual((setup.active_bundle(self.state) / "codex.key").stat().st_mode & 0o777, 0o600)
                self.assertFalse(old.exists())

    def test_an_absolute_mount_path_through_the_stable_link_keeps_reading_the_mount(self):
        self.seed()
        external = self.state / "mounted.key"
        external.write_text("fixture-mounted")
        reference = self.state / "setup" / ".." / external.name
        document = self.document()
        document["secrets"]["CODEX_API_KEY"] = str(reference)
        (setup.active_bundle(self.state) / "settings.json").write_text(json.dumps(document))
        settings.save(self.state, {"name": "REVIEW_POLL_SECONDS", "value": "60"})
        self.assertEqual(self.document()["secrets"]["CODEX_API_KEY"], str(reference))
        external.write_text("fixture-rotated")
        setup.apply_saved_setup(self.state)
        self.assertEqual(os.environ["CODEX_API_KEY"], "fixture-rotated")

    def test_an_unreadable_bundle_credential_leaves_the_active_setup_intact(self):
        self.seed()
        old = setup.active_bundle(self.state)
        credential = old / "codex.key"
        credential.unlink()
        mount = self.root / "reviewer-only-mount"
        credential.symlink_to(mount)
        document = self.document()
        document["secrets"]["CODEX_API_KEY"] = str(credential)
        (old / "settings.json").write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "Could not preserve the configured CODEX_API_KEY"):
            settings.save(self.state, {"name": "REVIEW_POLL_SECONDS", "value": "60"})
        self.assertEqual(setup.active_bundle(self.state), old)
        self.assertEqual(self.document(), document)
        self.assertEqual(list(self.state.glob(".review-setup-*")), [old])
        mount.write_text("fixture-mounted")
        setup.apply_saved_setup(self.state)
        self.assertEqual(os.environ["CODEX_API_KEY"], "fixture-mounted")

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
        replacement = dict(provider="github", url="https://github.com/org/replacement", checkout=str(self.checkout),
                           requester_ids=[84], visibility="private")
        with patch.dict(os.environ, {"REVIEW_PROJECTS": json.dumps([replacement])}):
            project, = settings.view(self.state)["projects"]
            self.assertEqual(project["url"], replacement["url"])
            rows = project["settings"]
            self.assertEqual(next(row["value"] for row in rows if row["name"] == "requester_ids"), "84")
            self.assertEqual({row["name"] for row in rows}, set(settings.PROJECT_FIELDS))
            self.assertEqual({row["locked"] for row in rows}, {True})
            with self.assertRaisesRegex(ValueError, "container environment"):
                settings.save(self.state, {"project": "https://github.com/org/repo", "name": "visibility", "value": "internal"})

    def test_provider_and_sign_in_overrides_control_available_credentials(self):
        self.seed(values={**SAVED, "REVIEW_AUTH_MODE": "login"})
        with patch.dict(os.environ, {"REVIEW_PROVIDER": "claude"}):
            view = settings.view(self.state)
            self.assertEqual(item(view, "REVIEW_PROVIDER")["value"], "Claude")
            self.assertEqual(item(view, "REVIEW_AUTH_MODE")["value"], "API key")
            self.assertEqual(item(view, "ANTHROPIC_API_KEY")["state"], "none")
            with self.assertRaisesRegex(ValueError, "Unknown credential"):
                settings.save(self.state, {"name": "CODEX_API_KEY", "value": "inactive-key"})
            settings.save(self.state, {"name": "ANTHROPIC_API_KEY", "value": "replacement"})
        self.assertEqual(self.document()["settings"]["REVIEW_PROVIDER"], "codex")
        with patch.dict(os.environ, {"REVIEW_PROVIDER": "claude", "REVIEW_AUTH_MODE": "login"}):
            view = settings.view(self.state)
            self.assertEqual(item(view, "REVIEW_AUTH_MODE")["value"], "Sign in with the CLI")
            with self.assertRaises(StopIteration):
                item(view, "ANTHROPIC_API_KEY")

    def test_overridden_rows_name_the_variable_and_never_its_value(self):
        self.seed()
        key = self.root / "mounted-codex.key"
        with patch.dict(os.environ, {"REVIEW_MAX_INPUT_TOKENS": "7000", "CODEX_API_KEY": "secret-from-env"}):
            view = settings.view(self.state)
            tokens, key_row = item(view, "REVIEW_MAX_INPUT_TOKENS"), item(view, "CODEX_API_KEY")
            self.assertIn("Set by REVIEW_MAX_INPUT_TOKENS in the environment", tokens["help"])
            self.assertIn("Set by CODEX_API_KEY in the environment", key_row["help"])
            self.assertTrue(key_row["locked"])
            self.assertNotIn("secret-from-env", json.dumps(view))
        with patch.dict(os.environ, {"CODEX_API_KEY_FILE": str(key)}):
            view = settings.view(self.state)
            self.assertIn("Set by CODEX_API_KEY_FILE in the environment", item(view, "CODEX_API_KEY")["help"])
            self.assertNotIn(str(key), json.dumps(view))
        with patch.dict(os.environ, {"REVIEW_PROJECTS": json.dumps(self.document()["projects"])}):
            project, = settings.view(self.state)["projects"]
            self.assertTrue(all("Set by REVIEW_PROJECTS in the environment" in row["help"] for row in project["settings"]))

    def test_provider_and_sign_in_rows_follow_the_effective_values(self):
        self.seed(values={**SAVED, "REVIEW_AUTH_MODE": "login"})
        view = settings.view(self.state)
        for name in ("REVIEW_PROVIDER", "REVIEW_AUTH_MODE"):
            self.assertFalse(item(view, name)["locked"])
            self.assertIn("setup wizard", item(view, name)["help"])
        with patch.dict(os.environ, {"REVIEW_PROVIDER": "claude"}):
            view = settings.view(self.state)
            provider, mode = item(view, "REVIEW_PROVIDER"), item(view, "REVIEW_AUTH_MODE")
            self.assertEqual((provider["value"], provider["locked"], provider["source"]), ("Claude", True, "env"))
            self.assertIn("Set by REVIEW_PROVIDER in the environment", provider["help"])
            # The saved sign-in method belongs to the saved provider, which is not the one in use.
            self.assertEqual((mode["value"], mode["locked"], mode["source"]), ("API key", True, "env"))
            self.assertIn("REVIEW_PROVIDER", mode["help"])
            self.assertIn("saved sign-in method does not apply", mode["help"])
        with patch.dict(os.environ, {"REVIEW_PROVIDER": "codex", "REVIEW_AUTH_MODE": "api-key"}):
            view = settings.view(self.state)
            mode = item(view, "REVIEW_AUTH_MODE")
            self.assertEqual((mode["value"], mode["locked"]), ("API key", True))
            self.assertIn("Set by REVIEW_AUTH_MODE in the environment", mode["help"])
            self.assertEqual(item(view, "CODEX_API_KEY")["state"], "stored")

    def test_gitlab_override_does_not_reuse_credentials_from_another_instance(self):
        original = dict(provider="gitlab", url="https://gitlab.com/org/repo", requester_ids=[13])
        self.seed(projects=[original], secrets={setup.TOKEN: "original-token"})
        overriding = dict(original, url="https://gitlab.example.org/org/repo")
        with patch.dict(os.environ, {"REVIEW_PROJECTS": json.dumps([overriding])}):
            token = item(settings.view(self.state), setup.TOKEN)
            self.assertTrue(token["locked"])
            self.assertEqual(token["state"], "none")
            self.assertEqual(settings.gitlab_access(self.document(), setup.active_bundle(self.state)),
                             ("https://gitlab.example.org", None, True))
            with self.assertRaisesRegex(ValueError, "different GitLab instance"):
                settings.save(self.state, {"name": setup.TOKEN, "value": "replacement"})

    def test_gitlab_override_loads_environment_tls_for_the_replacement_instance(self):
        original = dict(provider="gitlab", url="https://gitlab.com/org/repo", requester_ids=[13])
        self.seed(projects=[original], secrets={setup.TOKEN: "original-token", setup.gitlab_tls.CRL: REVOCATIONS})
        replacement = dict(original, url="https://gitlab.example.org/org/repo")
        certificate = self.state / "replacement-ca.pem"
        certificate.write_text(CERTIFICATE)
        with patch.dict(os.environ, {"REVIEW_PROJECTS": json.dumps([replacement]),
                                     setup.TOKEN: "replacement-token", setup.gitlab_tls.CA + "_FILE": str(certificate)}):
            site, token, verify = settings.gitlab_access(self.document(), setup.active_bundle(self.state))
        self.assertEqual((site, token), ("https://gitlab.example.org", "replacement-token"))
        self.assertIsInstance(verify, ssl.SSLContext)
        self.assertIn(ssl.PEM_cert_to_DER_cert(CERTIFICATE), verify.get_ca_certs(binary_form=True))
        self.assertFalse(verify.verify_flags & ssl.VERIFY_CRL_CHECK_LEAF)

    def test_gitlab_tls_is_only_loaded_for_edits_requiring_a_gitlab_lookup(self):
        gitlab_checkout = self.root / "gitlab-checkout"
        gitlab_checkout.mkdir()
        gitlab = dict(provider="gitlab", url="https://gitlab.com/org/other", requester_ids=[13],
                      checkout=str(gitlab_checkout))
        github = dict(provider="github", url="https://github.com/org/repo", requester_ids=[14],
                      checkout=str(self.checkout))
        self.seed(projects=[gitlab, github], secrets={setup.TOKEN: "fixture-token"})
        with patch.dict(os.environ, {setup.gitlab_tls.CA: NOT_A_CERTIFICATE}):
            settings.save(self.state, {"project": github["url"], "name": "visibility", "value": "public"})
            settings.save(self.state, {"project": gitlab["url"], "name": "requester_ids", "value": "15"})
            settings.save(self.state, {"name": "REVIEW_OPERATOR", "value": "alice:16"})
        rows = {row["provider"]: row for row in self.document()["projects"]}
        self.assertEqual(rows["github"]["visibility"], "public")
        self.assertEqual(rows["gitlab"]["requester_ids"], [15])
        self.assertEqual(self.document()["settings"]["REVIEW_OPERATOR"], "alice:16")

    def test_gitlab_lookup_loads_mounted_ca_and_crl(self):
        project = dict(provider="gitlab", url="https://gitlab.com/org/repo", requester_ids=[13])
        certificate, revocations = self.root / "mounted-ca.pem", self.root / "mounted-crl.pem"
        certificate.write_text(CERTIFICATE)
        revocations.write_text(REVOCATIONS)
        self.seed(projects=[project], secrets={setup.TOKEN: "fixture-token", setup.gitlab_tls.CA: str(certificate),
                                              setup.gitlab_tls.CRL: str(revocations)})
        _, _, verify = settings.gitlab_access(self.document(), setup.active_bundle(self.state))
        self.assertIn(ssl.PEM_cert_to_DER_cert(CERTIFICATE), verify.get_ca_certs(binary_form=True))
        self.assertTrue(verify.verify_flags & ssl.VERIFY_CRL_CHECK_LEAF)

    def test_gitlab_lookup_requires_an_unambiguous_instance_matching_environment(self):
        gitlab = dict(provider="gitlab", url="https://gitlab.com/org/repo", requester_ids=[13],
                      gitlab_base="https://gitlab.com")
        other = dict(gitlab, url="https://gitlab.example.org/org/repo", gitlab_base="https://gitlab.example.org")
        self.seed(projects=[gitlab], secrets={setup.TOKEN: "fixture-token"})
        for overrides in ({"REVIEW_PROJECTS": json.dumps([gitlab, other])},
                          {"REVIEW_PROJECTS": json.dumps([other]), "GITLAB_API_BASE": "https://gitlab.com/api/v4"},
                          {"REVIEW_PROJECTS": json.dumps([other]), "REVIEW_GITLAB_BASE_URL": "https://gitlab.com"}):
            with self.subTest(overrides=overrides), patch.dict(os.environ, {setup.TOKEN: "fixture-env", **overrides}):
                with self.assertRaisesRegex(ValueError, "GitLab instance|must match"):
                    settings.gitlab_access(self.document(), setup.active_bundle(self.state))

    def test_operator_lookup_does_not_persist_an_id_from_an_overridden_forge(self):
        gitlab = dict(provider="gitlab", url="https://gitlab.com/org/repo", requester_ids=[13])
        other = dict(gitlab, url="https://gitlab.example.org/org/repo")
        self.seed(projects=[gitlab], secrets={setup.TOKEN: "fixture-token"})
        with patch.dict(os.environ, {"REVIEW_PROJECTS": json.dumps([other]), setup.TOKEN: "fixture-env"}), \
                patch.object(accounts, "gitlab_id", return_value=94) as lookup:
            with self.assertRaisesRegex(ValueError, "username:numeric-id"):
                settings.save(self.state, {"name": "REVIEW_OPERATOR", "value": "bob"})
            lookup.assert_not_called()
            settings.save(self.state, {"name": "REVIEW_OPERATOR", "value": "bob:37"})
        self.assertEqual(self.document()["settings"]["REVIEW_OPERATOR"], "bob:37")

    def test_github_ids_are_saved_in_runtime_format(self):
        self.seed()
        for name in (setup.APP_ID, setup.INSTALL_ID):
            settings.save(self.state, {"name": name, "value": "0043"})
            self.assertEqual(self.document()["settings"][name], "43")

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
        with patch.object(providers, "team_command", return_value={"teams": teams}) as command, \
                patch.object(settings.tempfile, "TemporaryDirectory", side_effect=PermissionError("no writable temp")):
            settings.save(self.state, {"name": "REVIEW_CURSOR_TEAM_ID", "value": "1"})
            self.assertEqual([c.args[0] for c in command.call_args_list], [["list"]])
            # The live sign-in profile is listed, never switched.
            self.assertTrue(command.call_args.args[2].is_relative_to(self.state / "provider-login"))
        self.assertEqual(self.document()["settings"]["REVIEW_LOGIN_PROFILE"], profile)
        self.assertEqual(self.document()["settings"]["REVIEW_CURSOR_TEAM_ID"], "1")

    def test_cursor_team_change_with_an_overriding_key_also_checks_the_saved_key(self):
        team_seven = {"id": 7, "name": "Seven", "isSelectable": True}
        team_eight = {"id": 8, "name": "Eight", "isSelectable": True}
        env_key = self.root / "overriding-cursor-key"
        env_key.write_text("another-key")
        self.seed(values={**SAVED, "REVIEW_PROVIDER": "cursor", "REVIEW_CURSOR_TEAM_ID": "7"},
                  secrets={setup.CURSOR: "working-key"})
        old = setup.active_bundle(self.state)

        def list_teams(args, key, _):
            self.assertEqual(args, ["list"])
            return {"teams": [team_seven] if key == "working-key" else [team_seven, team_eight]}

        with patch.dict(os.environ, {setup.CURSOR + "_FILE": str(env_key)}), \
                patch.object(providers, "team_command", side_effect=list_teams):
            with self.assertRaisesRegex(ValueError, "saved Cursor account"):
                settings.save(self.state, {"name": "REVIEW_CURSOR_TEAM_ID", "value": "8"})
            self.assertEqual(setup.active_bundle(self.state), old)
            self.assertEqual(self.document()["settings"]["REVIEW_CURSOR_TEAM_ID"], "7")
            settings.save(self.state, {"name": "REVIEW_CURSOR_TEAM_ID", "value": "7"})
        self.assertEqual(self.document()["settings"]["REVIEW_CURSOR_TEAM_ID"], "7")

    def test_cursor_team_change_reports_a_saved_key_unavailable_in_setup(self):
        teams = [{"id": number, "name": str(number), "isSelectable": True} for number in (7, 8)]
        self.seed(values={**SAVED, "REVIEW_PROVIDER": "cursor", "REVIEW_CURSOR_TEAM_ID": "7"},
                  secrets={setup.CURSOR: str(self.root / "reviewer-only-mount")})
        old = setup.active_bundle(self.state)
        with patch.dict(os.environ, {setup.CURSOR: "fixture-overriding-key"}), \
                patch.object(providers, "team_command", return_value={"teams": teams}):
            with self.assertRaisesRegex(ValueError, "saved Cursor key is unavailable"):
                settings.save(self.state, {"name": "REVIEW_CURSOR_TEAM_ID", "value": "8"})
            self.assertEqual(setup.active_bundle(self.state), old)
            settings.save(self.state, {"name": "REVIEW_CURSOR_TEAM_ID", "value": "7"})
        self.assertEqual(self.document()["settings"]["REVIEW_CURSOR_TEAM_ID"], "7")

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

    def test_cursor_key_must_pass_team_selection_before_activating(self):
        self.seed(values={**SAVED, "REVIEW_PROVIDER": "cursor", "REVIEW_CURSOR_TEAM_ID": "7"},
                  secrets={setup.CURSOR: "working-key"})
        before = (self.state / "setup").readlink()
        with patch.object(providers, "select_team", side_effect=ValueError("Team unavailable")) as select:
            with self.assertRaisesRegex(ValueError, "setup wizard"):
                settings.save(self.state, {"name": setup.CURSOR, "value": "invalid-key"})
        self.assertEqual(select.call_args.args[:2], ("invalid-key", "7"))
        self.assertEqual((self.state / "setup").readlink(), before)
        self.assertEqual((setup.active_bundle(self.state) / "cursor.key").read_text().strip(), "working-key")
        with patch.object(providers, "select_team", return_value="8") as select, \
             patch.dict(os.environ, {"REVIEW_CURSOR_TEAM_ID": "8"}):
            settings.save(self.state, {"name": setup.CURSOR, "value": "replacement"})
        self.assertEqual([call.args[:2] for call in select.call_args_list], [("replacement", "8"), ("replacement", "7")])
        self.assertEqual((setup.active_bundle(self.state) / "cursor.key").read_text().strip(), "replacement")

    def test_cursor_key_change_with_an_overriding_team_also_checks_the_saved_team(self):
        self.seed(values={**SAVED, "REVIEW_PROVIDER": "cursor", "REVIEW_CURSOR_TEAM_ID": "7"},
                  secrets={setup.CURSOR: "working-key"})
        old = setup.active_bundle(self.state)

        def only_overriding_team_available(key, team, _):
            self.assertEqual(key, "only-team-eight")
            if team != "8":
                raise ValueError("The selected team ID is not eligible for this Cursor account")
            return team

        with patch.dict(os.environ, {"REVIEW_CURSOR_TEAM_ID": "8"}), \
                patch.object(providers, "select_team", side_effect=only_overriding_team_available):
            with self.assertRaisesRegex(ValueError, "setup wizard"):
                settings.save(self.state, {"name": setup.CURSOR, "value": "only-team-eight"})
        self.assertEqual(setup.active_bundle(self.state), old)
        self.assertEqual((old / "cursor.key").read_text().strip(), "working-key")

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
                            "owner:13", "", "", "", "", "codex", "", "", "paste"], ["fixture-forge"])
        settings.save(self.state, {"name": "REVIEW_SYNC_SECONDS", "value": "300"})
        settings.save(self.state, {"name": "REVIEW_OSV_DOWNLOAD_DATABASES", "value": "0"})
        bundle = setup.active_bundle(self.state) / "settings.json"
        config = json.loads(bundle.read_text())
        config["settings"]["REVIEW_MAX_FINDINGS_MID"] = "4"
        bundle.write_text(json.dumps(config))
        setup.apply_saved_setup(self.state)
        self.assertEqual((os.environ["REVIEW_SYNC_SECONDS"], os.environ["REVIEW_OSV_DOWNLOAD_DATABASES"]), ("300", "0"))
        old = setup.active_bundle(self.state)
        remove = shutil.rmtree

        def remove_bundle(path):
            if path == old:
                raise PermissionError("fixture cleanup error")
            return remove(path)

        with patch.object(shutil, "rmtree", side_effect=remove_bundle), contextlib.redirect_stderr(io.StringIO()) as warning:
            wizard(self.state, ["y", "", "", "", "", "", "", "", "", "keep"], [])
        self.assertIn("Settings were saved", warning.getvalue())
        self.assertNotEqual(setup.active_bundle(self.state), old)
        kept = self.document()["settings"]
        self.assertEqual((kept["REVIEW_SYNC_SECONDS"], kept["REVIEW_OSV_DOWNLOAD_DATABASES"]), ("300", "0"))
        self.assertEqual(kept["REVIEW_MAX_FINDINGS_MID"], "4")


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

    def test_terminal_quit_waits_for_settings_save_to_complete(self):
        session = Session()
        page = settings.view(self.state)
        session.show(page)
        started, finished = threading.Event(), threading.Event()

        def close():
            started.set()
            session.terminal_quit()
            finished.set()

        with session.editing:
            thread = threading.Thread(target=close)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertFalse(finished.is_set())
            session.show(page, "Saved.")
        self.assertTrue(finished.wait(timeout=2))
        thread.join(timeout=2)
        self.assertEqual(session.view["status"], "quit")

    def test_wizard_save_recovers_from_failed_readback(self):
        session = Session()
        session.show(settings.view(self.state))
        with patch.object(setup, "configure"), patch.object(settings, "view", side_effect=OSError("unreadable")):
            session.launch(self.state, resume=True)
            self.settled(session, "settings")
        self.assertTrue(session.view["unread"])
        self.assertEqual(session.view["messages"], ["The setup wizard saved the configuration.", web_setup.UNREADABLE])
        session.reread(self.state)
        self.assertNotIn("unread", session.view)

    def test_a_save_that_commits_is_reported_saved_even_if_the_page_cannot_be_read_back(self):
        session = Session()
        session.show(settings.view(self.state))
        rendered = session.view["id"]
        change = {"id": rendered, "name": "REVIEW_MAX_INPUT_TOKENS", "value": "9000"}
        with patch.object(settings, "view", side_effect=OSError("settings.json vanished")):
            code, reply = change_setting(session, self.state, change)
        self.assertEqual(code, 200)
        self.assertTrue(reply["unread"])
        self.assertEqual(reply["messages"], [web_setup.SAVED, web_setup.UNREADABLE])
        self.assertNotIn("vanished", json.dumps(reply))
        self.assertEqual(json.loads((self.state / "setup" / "settings.json").read_text())["settings"]["REVIEW_MAX_INPUT_TOKENS"], "9000")
        # The page rendered before the save is stale, and a reload reads the settings again.
        self.assertEqual(change_setting(session, self.state, change), (409, None))
        with patch.object(settings, "view", side_effect=ValueError("still unreadable")):
            session.reread(self.state)
        self.assertTrue(session.view["unread"])
        self.assertEqual(session.view["messages"], [web_setup.UNREADABLE])
        session.reread(self.state)
        self.assertNotIn("unread", session.view)
        self.assertEqual(item(session.view, "REVIEW_MAX_INPUT_TOKENS")["value"], "9000")

    def test_a_committed_save_is_reported_saved_when_the_old_bundle_cannot_be_removed(self):
        session = Session()
        session.show(settings.view(self.state))
        old = setup.active_bundle(self.state)
        with patch.object(settings.shutil, "rmtree", side_effect=PermissionError("busy")):
            code, reply = change_setting(session, self.state, {"id": session.view["id"], "name": "REVIEW_MAX_INPUT_TOKENS", "value": "9000"})
        self.assertEqual(code, 200)
        self.assertEqual(reply["messages"], [web_setup.SAVED, "The previous settings bundle could not be removed from the state volume."])
        self.assertNotEqual(setup.active_bundle(self.state), old)
        self.assertEqual(item(reply, "REVIEW_MAX_INPUT_TOKENS")["value"], "9000")

    def test_older_browser_view_cannot_overwrite_a_newer_saved_bundle(self):
        session = Session()
        session.show(settings.view(self.state))
        stale = {"id": session.view["id"], "name": "REVIEW_MAX_INPUT_TOKENS", "value": "8000"}
        settings.save(self.state, {"name": "REVIEW_MAX_INPUT_TOKENS", "value": "9000"})
        self.assertEqual(change_setting(session, self.state, stale), (409, None))
        session.reread(self.state)
        self.assertEqual(item(session.view, "REVIEW_MAX_INPUT_TOKENS")["value"], "9000")
        code, refreshed = change_setting(session, self.state, {**stale, "id": session.view["id"]})
        self.assertEqual(code, 200)
        self.assertEqual(item(refreshed, "REVIEW_MAX_INPUT_TOKENS")["value"], "8000")

    def test_reloading_settings_reads_the_latest_bundle(self):
        session = Session()
        first = session.show(settings.view(self.state))
        session.reread(self.state)
        self.assertEqual(session.view["id"], first["id"])
        settings.save(self.state, {"name": "REVIEW_MAX_INPUT_TOKENS", "value": "9000"})
        session.reread(self.state)
        self.assertNotEqual(session.view["id"], first["id"])
        self.assertEqual(item(session.view, "REVIEW_MAX_INPUT_TOKENS")["value"], "9000")

    def test_activation_does_not_unlink_the_consumed_temporary_link(self):
        session = Session()
        session.show(settings.view(self.state))
        unlink = Path.unlink

        def failure_after_replace(path, *args, **kwargs):
            if path.name.startswith(".setup-link-"):
                raise PermissionError("cleanup failed")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", failure_after_replace):
            code, reply = change_setting(session, self.state, {"id": session.view["id"],
                                         "name": "REVIEW_MAX_INPUT_TOKENS", "value": "9000"})
        self.assertEqual(code, 200)
        self.assertTrue(setup.active_bundle(self.state).is_dir())
        self.assertEqual(item(reply, "REVIEW_MAX_INPUT_TOKENS")["value"], "9000")

    def test_a_save_that_fails_is_reported_unsaved(self):
        session = Session()
        session.show(settings.view(self.state))
        before = (self.state / "setup").readlink()
        code, reply = change_setting(session, self.state, {"id": session.view["id"], "name": "REVIEW_MAX_INPUT_TOKENS", "value": "0"})
        self.assertEqual((code, reply), (400, {"error": "Enter a positive integer"}))
        self.assertEqual((self.state / "setup").readlink(), before)
        with patch.object(settings, "save", side_effect=RuntimeError("internal detail")):
            code, reply = change_setting(session, self.state, {"id": session.view["id"], "name": "REVIEW_MAX_INPUT_TOKENS", "value": "9"})
        self.assertEqual(code, 400)
        self.assertNotIn("internal detail", reply["error"])

    def test_the_settings_route_reports_a_committed_save_whose_page_cannot_be_read(self):
        key, real, calls = "fixture-setup-key", settings.view, []

        def view(state):
            calls.append(state)
            if len(calls) == 2:  # the read right after the save
                raise OSError("settings.json vanished")
            return real(state)

        def run():
            try:
                web_setup.serve(self.state)
            except dialog.QuitSetup:
                pass

        with patch.object(web_setup.secrets, "token_urlsafe", return_value=key), patch.object(settings, "view", side_effect=view), \
             patch("sys.stdin", io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            server = threading.Thread(target=run, daemon=True)
            server.start()

            def request(method="GET", path="/step", headers=None, body=None):
                conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
                try:
                    conn.request(method, path, headers=headers or {}, body=body)
                    res = conn.getresponse()
                    return res.status, res.read()
                finally:
                    conn.close()

            origin = {"Origin": "http://127.0.0.1:8765", "Content-Type": "application/json"}
            for _ in range(100):
                try:
                    status, _ = request("POST", "/login", origin, json.dumps({"key": key}))
                    break
                except ConnectionError:
                    threading.Event().wait(0.02)
            self.assertEqual(status, 200)
            headers = {**origin, "Cookie": f"pr_agent_setup={key}"}
            page = json.loads(request(headers=headers)[1])
            change = json.dumps({"id": page["id"], "name": "REVIEW_MAX_INPUT_TOKENS", "value": "9000"})
            # The usual guards still apply to the route.
            self.assertEqual(request("POST", "/setting", {k: v for k, v in headers.items() if k != "Cookie"}, change)[0], 401)
            self.assertEqual(request("POST", "/setting", {k: v for k, v in headers.items() if k != "Origin"}, change)[0], 403)
            status, body = request("POST", "/setting", headers, change)
            self.assertEqual(status, 200)
            reply = json.loads(body)
            self.assertTrue(reply["unread"])
            self.assertEqual(reply["messages"][0], web_setup.SAVED)
            self.assertEqual(json.loads((self.state / "setup" / "settings.json").read_text())["settings"]["REVIEW_MAX_INPUT_TOKENS"], "9000")
            self.assertEqual(request("POST", "/setting", headers, change)[0], 409)
            reloaded = json.loads(request(headers=headers)[1])
            self.assertNotIn("unread", reloaded)
            self.assertEqual(item(reloaded, "REVIEW_MAX_INPUT_TOKENS")["value"], "9000")
            self.assertEqual(request("POST", "/step", headers, json.dumps({"id": reloaded["id"], "action": "quit"}))[0], 200)
            server.join(timeout=5)
            self.assertFalse(server.is_alive())

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
