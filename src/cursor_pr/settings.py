"""Change one setting of a saved setup without re-running the setup wizard."""
import copy
import fcntl
import json
import os
from pathlib import Path
import shutil
import ssl
import tempfile

from . import accounts, gitlab_tls, providers, setup
from .config import DEFAULT_MAX_INPUT_TOKENS, gitlab_base, parse_projects
from .credentials import APP_ID, INSTALL_ID, KEY, TOKEN, private_file, read_secret, validate_operator, validate_pem, validate_token
from .login import login_directory, login_env

FIELDS = (
    {"name": "REVIEW_MAX_INPUT_TOKENS", "section": "Timing and limits", "label": "Maximum input tokens per review",
     "help": "Reviews whose estimated prompt exceeds this many tokens are skipped as over-budget.",
     "default": str(DEFAULT_MAX_INPUT_TOKENS)},
    {"name": "REVIEW_POLL_SECONDS", "section": "Timing and limits", "label": "Seconds between polls",
     "help": "How often the reviewer looks for new /review comments.", "default": "120"},
    {"name": "REVIEW_SYNC_SECONDS", "section": "Timing and limits", "label": "Seconds between repository syncs",
     "help": "How often the sync service refreshes managed checkouts.", "default": "120"},
    {"name": "REVIEW_QUALGEN_INTERVAL_SECONDS", "section": "Timing and limits", "label": "Seconds between history refreshes",
     "help": "How often Qualgen mines repository history. 0 disables it.", "default": "86400"},
    {"name": "REVIEW_OSV_DOWNLOAD_DATABASES", "section": "Timing and limits", "label": "OSV advisory database downloads",
     "help": "Whether OSV-Scanner may download public advisory databases.", "kind": "choice", "default": "1",
     "choices": (("1", "Allow downloads"), ("0", "Offline"))},
    {"name": "REVIEW_PROVIDER", "section": "Review provider", "label": "Review provider", "wizard": True,
     "help": "Switching provider needs a sign-in or an API key, so change it in the setup wizard."},
    {"name": "REVIEW_MODEL", "section": "Review provider", "label": "Model", "kind": "text", "default": "auto",
     "help": "A model the selected provider offers. auto uses its default model."},
    {"name": "REVIEW_CURSOR_TEAM_ID", "section": "Review provider", "label": "Cursor billing team", "kind": "text",
     "when": "cursor", "help": "Numeric ID of the team that pays for reviews. Leave blank for personal scope or a single team."},
    {"name": "REVIEW_AUTH_MODE", "section": "Review provider", "label": "Sign-in method", "wizard": True,
     "help": "Switching between CLI sign-in and an API key needs a sign-in or a key, so change it in the setup wizard."},
    {"name": "REVIEW_OPERATOR", "section": "Operator", "label": "Responsible human", "kind": "text",
     "help": "The operator's forge username, or username:numeric-id. With GitHub and GitLab configured, use username:numeric-id."},
    {"name": APP_ID, "section": "GitHub", "label": "GitHub App ID", "when": "github",
     "help": "The App whose private key is stored with this setup."},
    {"name": INSTALL_ID, "section": "GitHub", "label": "GitHub App installation ID", "when": "github",
     "help": "The installation of that App which covers the repositories."},
)
VISIBILITIES = (("private", "Private"), ("internal", "Internal"), ("public", "Public"))
GITLAB_CREDENTIALS = (TOKEN, *gitlab_tls.NAMES)
PROJECT_FIELDS = ("requester_ids", "visibility", "go_analysis", "scanner_ci")
PROJECT_ROW_FIELDS = (
    ("requester_ids", "Who can request reviews", "text", "GitHub or GitLab usernames or numeric account IDs, separated by commas."),
    ("visibility", "Visibility", "choice", "Set the actual visibility to authorize posting on internal or public projects."),
    ("go_analysis", "Trusted Go analysis producer", "text", "A GitHub Actions workflow filename or ID, or a GitLab CI job name. Leave blank to skip."),
    ("scanner_ci", "Scanner CI reports", "json", "JSON overrides per scanner (hadolint, zizmor, osv-scanner) that reuse your CI reports. Leave blank for defaults."))
LOCATION_HELP = {
    "stored": "Stored on the state volume. Replace it by pasting a new value; it is never shown.",
    "mounted": "Read from the mounted file shown here. Replace it to keep a copy on the state volume instead.",
    "none": "Not set."}


class StaleSettings(ValueError):
    """A different installer has saved since the settings view was read."""


def from_environment(name, what="the saved value"):
    """Why a row is read-only: the page's own process environment sets it. Never includes the value."""
    # Only this process's environment is visible here; each service reads its own.
    return (f"Set by {name} in the environment of the process serving this page, which overrides {what}. "
            "Change or remove it there. The sync and reviewer services read their own environment.")


def plain(rule):
    """Adapt a validator that only looks at the value."""
    return lambda value, config, state, old: rule(value)


def choice(values):
    def validate(value):
        if value not in values:
            raise ValueError("Select one of the displayed options")
        return value
    return validate


def stored(config, old, name):
    """The current value of a credential, from the container environment or the bundle."""
    if name in os.environ or name + "_FILE" in os.environ:
        return read_secret(name)
    location = config["secrets"].get(name)
    if not location:
        raise ValueError(f"Set {name} or {name}_FILE")
    return (old / location).read_text().strip()


def gitlab_access(config, old):
    """The instance, reviewer token and TLS context that GitLab username lookups need."""
    instances = setup.gitlab_instances(setup.effective_projects(config))
    if not instances:
        return None, None, True
    if len(instances) != 1:
        raise ValueError("Configure one GitLab instance per container")
    base, = instances
    api_base = os.getenv("GITLAB_API_BASE", "").rstrip("/")
    if api_base and api_base != base + "/api/v4":
        raise ValueError("GITLAB_API_BASE must match the selected projects")
    if os.getenv("REVIEW_GITLAB_BASE_URL") and gitlab_base(os.environ["REVIEW_GITLAB_BASE_URL"]) != base:
        raise ValueError("REVIEW_GITLAB_BASE_URL must match the selected projects")
    same_instances = instances == setup.gitlab_instances(config["projects"])
    try:
        token = accounts.saved_token()
        if token is None and same_instances:
            token = stored(config, old, TOKEN)
    except ValueError:
        token = None
    trust = [name for name in gitlab_tls.NAMES
             if name in os.environ or name + "_FILE" in os.environ or (same_instances and name in config["secrets"])]
    verify = True
    if trust:
        with tempfile.TemporaryDirectory(prefix="review-gitlab-settings-") as temp:
            bundle = Path(temp)
            for name in trust:
                private_file(bundle / gitlab_tls.NAMES[name], gitlab_tls.validate(name, stored(config, old, name)))
            try:
                verify = gitlab_tls.context(bundle)
            except ssl.SSLError:
                raise ValueError("The GitLab certificate files could not be loaded for TLS") from None
    return base, token, verify


def model(value, config, state, old):
    return None if value in ("", "auto") else value


def cursor_login_home(state, profile):
    return Path(login_env("cursor", login_directory(state, profile))["HOME"])


def check_cursor_team(account, value):
    key, home = account
    if home is not None:
        return providers.eligible_team(key, value, home)
    with tempfile.TemporaryDirectory(prefix="review-team-check-") as temp:
        return providers.eligible_team(key, value, Path(temp))


def cursor_team(value, config, state, old):
    saved = config["settings"]
    saved_backend = saved.get("REVIEW_PROVIDER", "cursor")
    saved_profile = saved.get("REVIEW_LOGIN_PROFILE", "")
    backend, mode = setup.effective_backend(saved)
    profile = os.getenv("REVIEW_LOGIN_PROFILE", saved_profile if backend == saved_backend else "")
    # Inspect team membership without switching the live sign-in profile.
    effective = ((stored(config, old, setup.CURSOR), None) if mode == "api-key"
                 else (None, cursor_login_home(state, profile)))
    chosen = check_cursor_team(effective, value)
    if saved_backend != "cursor" or chosen == saved.get("REVIEW_CURSOR_TEAM_ID", ""):
        return chosen
    if saved.get("REVIEW_AUTH_MODE") == "login":
        fallback = (None, cursor_login_home(state, saved_profile))
    elif location := config["secrets"].get(setup.CURSOR):
        try:
            fallback = ((old / location).read_text().strip(), None)
        except OSError:
            raise ValueError("The saved Cursor key is unavailable here. Make it readable before changing the shared team.") from None
    else:
        return chosen  # The saved API-key setup requires an environment-supplied key.
    if fallback != effective:
        try:
            check_cursor_team(fallback, chosen)
        except ValueError as err:
            raise ValueError(f"The saved Cursor account cannot use that team. {err}") from None
    return chosen


def operator(value, config, state, old):
    if ":" in value:
        return validate_operator(value)
    effective = setup.effective_projects(config)
    forges = {row["provider"] for row in effective}
    saved_forges = {row["provider"] for row in config["projects"]}
    if forges != saved_forges or (forges == {"gitlab"} and
                                 setup.gitlab_instances(effective) != setup.gitlab_instances(config["projects"])):
        raise ValueError("Repository overrides change the forge for lookup. Enter username:numeric-id for the saved setup.")
    if forges != {"gitlab"}:
        return validate_operator(accounts.resolve_operator(value, forges))
    base, token, verify = gitlab_access(config, old)
    return validate_operator(accounts.resolve_operator(value, forges, base=base, token=token, verify=verify))


VALIDATORS = {
    "REVIEW_MAX_INPUT_TOKENS": plain(setup.number), "REVIEW_POLL_SECONDS": plain(setup.number),
    "REVIEW_SYNC_SECONDS": plain(setup.number), "REVIEW_QUALGEN_INTERVAL_SECONDS": plain(setup.nonnegative),
    "REVIEW_OSV_DOWNLOAD_DATABASES": plain(choice(("1", "0"))), "REVIEW_MODEL": model,
    "REVIEW_CURSOR_TEAM_ID": cursor_team, "REVIEW_OPERATOR": operator,
    APP_ID: plain(lambda value: setup.number(value).lstrip("0")),
    INSTALL_ID: plain(lambda value: setup.number(value).lstrip("0"))}


def global_rows(config):
    saved = config["settings"]
    backend, mode = setup.effective_backend(saved)
    forges = {row["provider"] for row in setup.effective_projects(config)}
    rows = []
    for field in FIELDS:
        if field.get("when") == "cursor" and backend != "cursor" or field.get("when") == "github" and "github" not in forges:
            continue
        row = {key: field[key] for key in ("name", "section", "label", "help") if key in field}
        env = field["name"] in os.environ
        if env:
            row["help"] = from_environment(field["name"])
        if field.get("wizard"):
            # The page shows the provider and sign-in method this process runs with, not only the saved ones.
            replaced = (field["name"] == "REVIEW_AUTH_MODE" and not env
                        and backend != saved.get("REVIEW_PROVIDER", "cursor"))
            if replaced:
                row["help"] = from_environment("REVIEW_PROVIDER", "the saved provider, so its saved sign-in method "
                                               "does not apply and an API key is used unless REVIEW_AUTH_MODE is also set")
            row.update(kind="text", wizard=True, locked=env or replaced,
                       source="env" if env or replaced else "saved" if field["name"] in saved else "default",
                       value=providers.LABELS[backend] if field["name"] == "REVIEW_PROVIDER" else
                       {"login": "Sign in with the CLI", "api-key": "API key"}[mode])
        else:
            row.update(kind=field.get("kind", "number"), locked=env,
                       source="env" if env else "saved" if field["name"] in saved else "default",
                       value=os.environ[field["name"]] if env else saved.get(field["name"], field.get("default", "")))
            if "choices" in field:
                row["choices"] = field["choices"]
        rows.append(row)
    return rows


def credentials(config):
    """The credentials the page offers: name, label and whether each one is optional."""
    saved = config["settings"]
    backend, mode = setup.effective_backend(saved)
    forges = {row["provider"] for row in setup.effective_projects(config)}
    rows = []
    if mode == "api-key":
        rows.append((providers.KEYS[backend], providers.LABELS[backend] + " API key", False))
    if "github" in forges:
        rows.append((KEY, "GitHub App private key", False))
    if "gitlab" in forges:
        rows += [(TOKEN, "GitLab reviewer token", False), (gitlab_tls.CA, "GitLab private CA certificate", True),
                 (gitlab_tls.CRL, "GitLab certificate revocation list", True)]
    return rows


def credential_rows(config):
    """One row per credential, with where it lives but never its value."""
    rows = []
    for name, label, optional in credentials(config):
        different_instance = name in GITLAB_CREDENTIALS and (
            setup.gitlab_instances(setup.effective_projects(config)) != setup.gitlab_instances(config["projects"]))
        location = None if different_instance else config["secrets"].get(name)
        variable = next((v for v in (name, name + "_FILE") if v in os.environ), None)
        where = ("env" if variable
                 else "mounted" if location and Path(location).is_absolute() else "stored" if location else "none")
        rows.append({"name": name, "section": "Credentials", "label": label, "kind": "secret", "optional": optional,
                     "multiline": name not in (TOKEN, *providers.KEYS.values()), "state": where,
                     "locked": where == "env" or different_instance,
                     "value": location if where == "mounted" else "",
                     "help": from_environment(variable, "the saved credential") if variable
                     else "Use the setup wizard or container environment for a different GitLab instance."
                     if different_instance else LOCATION_HELP[where]})
    return rows


def project_rows(config):
    locked = "REVIEW_PROJECTS" in os.environ
    projects = []
    for row in setup.effective_projects(config):
        values = {"requester_ids": ", ".join(map(str, row["requester_ids"])), "visibility": row.get("visibility", "private"),
                  "go_analysis": row.get("go_analysis", ""), "scanner_ci": json.dumps(row.get("scanner_ci", {}), indent=2)}
        fields = []
        for name, label, kind, help in PROJECT_ROW_FIELDS:
            field = {"name": name, "label": label, "kind": kind, "locked": locked, "value": values[name],
                     "help": from_environment("REVIEW_PROJECTS", "the saved repositories") if locked else help}
            if kind == "choice":
                field["choices"] = VISIBILITIES
            fields.append(field)
        projects.append({"url": row["url"], "provider": row["provider"], "settings": fields})
    return projects


def view(state):
    """What the settings page shows. Credential values never leave the state volume."""
    with setup.locked(state, fcntl.LOCK_SH):
        bundle = setup.active_bundle(state)
        config = json.loads((bundle / "settings.json").read_text())
    sections = []
    for row in [*global_rows(config), *credential_rows(config)]:
        section = row.pop("section")
        if not sections or sections[-1]["title"] != section:
            sections.append({"title": section, "settings": []})
        sections[-1]["settings"].append(row)
    return {"revision": bundle.name, "sections": sections, "projects": project_rows(config)}


def edit_setting(config, state, old, name, value):
    if name not in VALIDATORS or name not in {row["name"] for row in global_rows(config)}:
        raise ValueError("Unknown setting")
    if name in os.environ:
        raise ValueError(f"{name} is supplied by the container environment")
    result = VALIDATORS[name](value, config, state, old)
    if result is None:
        config["settings"].pop(name, None)
    else:
        config["settings"][name] = result


def edit_project(config, state, old, url, name, value):
    if "REVIEW_PROJECTS" in os.environ:
        raise ValueError("Repositories are supplied by the container environment")
    if name not in PROJECT_FIELDS:
        raise ValueError("Unknown project setting")
    rows = config["projects"]
    row = next((row for row in rows if row["url"] == url), None)
    if row is None:
        raise ValueError("Unknown repository")
    if name == "requester_ids":
        row["requester_ids"] = [accounts.requester_id(x) for x in value.split(",") if x.strip()]
    elif name == "scanner_ci":
        try:
            row["scanner_ci"] = json.loads(value or "{}")
        except ValueError:
            raise ValueError("Scanner CI settings must be a JSON object") from None
        if not isinstance(row["scanner_ci"], dict):
            raise ValueError("Scanner CI settings must be a JSON object")
        if not row["scanner_ci"]:
            row.pop("scanner_ci")
    elif value or name != "go_analysis":
        row[name] = value
    else:
        row.pop("go_analysis", None)
    setup.validate_rows(rows)
    token, verify = None, True
    if accounts.pending(row for row in rows if row["provider"] == "gitlab"):
        _, token, verify = gitlab_access(config, old)
    rows = accounts.resolve(rows, token=token, verify=verify)
    if accounts.pending(rows):
        raise ValueError("GitLab usernames need the reviewer token; enter numeric IDs for: " + ", ".join(accounts.pending(rows)))
    parse_projects(rows)
    config["projects"] = rows


def replace_credential(config, name, value, remove, written):
    if name not in {row[0] for row in credentials(config)}:
        raise ValueError("Unknown credential")
    if name in os.environ or name + "_FILE" in os.environ:
        raise ValueError(f"{name} is supplied by the container environment")
    if name in GITLAB_CREDENTIALS and (
            setup.gitlab_instances(setup.effective_projects(config)) != setup.gitlab_instances(config["projects"])):
        raise ValueError("Use the setup wizard or container environment for a different GitLab instance")
    if remove:
        if name not in gitlab_tls.NAMES or name not in config["secrets"]:
            raise ValueError("This credential cannot be removed")
        del config["secrets"][name]
        return
    written[name] = (validate_pem(value) if name == KEY
                     else gitlab_tls.validate(name, value) if name in gitlab_tls.NAMES else validate_token(value))
    if name == setup.CURSOR:
        saved = config["settings"]
        saved_team = saved.get("REVIEW_CURSOR_TEAM_ID", "")
        teams = [os.getenv("REVIEW_CURSOR_TEAM_ID", saved_team)]
        if saved.get("REVIEW_PROVIDER", "cursor") == "cursor" and saved.get("REVIEW_AUTH_MODE") != "login":
            teams.append(saved_team)
        with tempfile.TemporaryDirectory(prefix="review-team-check-") as temp:
            try:
                for team in dict.fromkeys(teams):
                    providers.select_team(written[name], team, Path(temp))
            except ValueError as err:
                raise ValueError(f"{err} Use the setup wizard if the key and team need to change together.") from None
    config["secrets"][name] = setup.SECRETS[name]


def references_bundle(path, old, replacement):
    """Would an absolute credential path lose part of its resolution after activation?"""
    parent = old.parent.resolve()
    retired, staged, active = parent / old.name, parent / replacement.name, parent / "setup"
    directory = Path("/")
    remaining = list(reversed(path.parts[1:]))
    links = 0
    while remaining:
        name = remaining.pop()
        if name == "..":
            directory = directory.parent
            continue
        candidate = directory / name
        if candidate == retired or directory == staged:
            return True
        if candidate == active:
            # Substitute the new target so /setup/../secrets remains a live mount.
            directory = staged
            continue
        try:
            target = candidate.readlink()
        except OSError:
            directory = candidate
            continue
        links += 1
        if links > 40:  # Bound traversal of broken references with link cycles.
            return False
        if target.is_absolute():
            directory = Path("/")
            remaining.extend(reversed(target.parts[1:]))
        else:
            remaining.extend(reversed(target.parts))
    return False


def save(state, change, *, revision=None):
    """Change one setting and report whether the previous bundle was removed."""
    name, value, project = change.get("name"), change.get("value", ""), change.get("project")
    if not isinstance(name, str) or not isinstance(value, str) or project is not None and not isinstance(project, str):
        raise ValueError("Unknown setting")
    value = value.strip()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with setup.installer_lock(state):
        with setup.locked(state, fcntl.LOCK_SH):
            old = setup.active_bundle(state)
            if not old:
                raise ValueError("Run setup before changing its settings.")
            if revision is not None and old.name != revision:
                raise StaleSettings("The settings changed in another installer")
            config = json.loads((old / "settings.json").read_text())
        config = copy.deepcopy(config)
        written = {}
        if name in setup.SECRETS:
            replace_credential(config, name, value, change.get("remove") is True, written)
        elif project is not None:
            edit_project(config, state, old, project, name, value)
        else:
            edit_setting(config, state, old, name, value)
        bundle = Path(tempfile.mkdtemp(dir=state, prefix=".review-setup-"))
        try:
            for secret, location in config["secrets"].items():
                if secret not in setup.SECRETS:
                    raise ValueError("Unknown credential in saved setup")
                if secret in written:
                    private_file(bundle / setup.SECRETS[secret], written[secret])
                elif not Path(location).is_absolute() or references_bundle(Path(location), old, bundle):
                    # Copy credentials whose configured paths would break when the old bundle is removed.
                    destination = bundle / setup.SECRETS[secret]
                    try:
                        shutil.copyfile(old / location, destination)
                        destination.chmod(0o600)
                    except OSError:
                        raise ValueError(f"Could not preserve the configured {secret} in a new setup bundle.") from None
                    config["secrets"][secret] = setup.SECRETS[secret]
            if written.keys() & gitlab_tls.NAMES.keys():
                try:
                    gitlab_tls.context(bundle)  # as the wizard does: refuse PEM data that TLS cannot load
                except ssl.SSLError:
                    raise ValueError("The GitLab certificate files could not be loaded for TLS") from None
            private_file(bundle / "settings.json", json.dumps(config, indent=2))
            setup.commit(state, old, bundle)
        except BaseException:
            shutil.rmtree(bundle)
            raise
        try:
            shutil.rmtree(old)
        except OSError:
            return False
        return True
