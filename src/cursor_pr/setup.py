"""Interactive configuration of a persistent reviewer state volume."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import termios
from urllib.parse import urlsplit
import uuid

from . import accounts, dialog, providers, provision, gitlab_tls
from .login import auth_mode, ensure_session, login_directory, login_env
from .config import (DEFAULT_MAX_FINDINGS, DEFAULT_MAX_INPUT_TOKENS, DEFAULT_VISUAL_MAX_INPUT_TOKENS, FINDINGS_SETTINGS,
                     MAX_FINDINGS_LIMIT, parse_projects, project_base, prompt_config)
from .credentials import APP_ID, INSTALL_ID, KEY, TOKEN, private_file, read_secret, validate_pem, validate_token

CURSOR = "CURSOR_API_KEY"
SECRETS = {CURSOR: "cursor.key", "ANTHROPIC_API_KEY": "claude.key", "CODEX_API_KEY": "codex.key", KEY: "github.pem", TOKEN: "gitlab.token", **gitlab_tls.NAMES}
# Carry saved settings that the wizard does not ask for into the next bundle.
PRESERVED_SETTINGS = (*FINDINGS_SETTINGS.values(), "REVIEW_SYNC_SECONDS", "REVIEW_OSV_DOWNLOAD_DATABASES")
SETTINGS = ("REVIEW_OPERATOR", APP_ID, INSTALL_ID, "REVIEW_MODEL", "REVIEW_MAX_INPUT_TOKENS",
            "REVIEW_VISUAL_MAX_INPUT_TOKENS", "REVIEW_POLL_SECONDS", "REVIEW_QUALGEN_INTERVAL_SECONDS", "REVIEW_PROVIDER",
            "REVIEW_CURSOR_TEAM_ID", "REVIEW_AUTH_MODE", "REVIEW_LOGIN_PROFILE", *PRESERVED_SETTINGS)


def state_dir():
    return Path(os.getenv("REVIEW_STATE_DIR", Path.home() / ".local/state/cursor-review"))


@contextmanager
def locked(state, operation):
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, operation)
        yield
    finally:
        os.close(fd)


def retire_profiles(state, preserve):
    profiles = login_directory(state, "") / "profiles"
    if not profiles.is_dir():
        return
    for path in profiles.iterdir():
        if (len(path.name) != 32 or any(c not in "0123456789abcdef" for c in path.name)
                or path.name in preserve or path.is_symlink() or not path.is_dir()):
            continue
        try:
            with locked(path, fcntl.LOCK_EX | fcntl.LOCK_NB):
                shutil.rmtree(path)
        except (BlockingIOError, FileNotFoundError):
            continue


def active_bundle(state):
    link = state / "setup"
    if not link.is_symlink():
        if link.exists():
            raise ValueError(f"{link} already exists and is not a setup-managed link")
        return None
    target = link.readlink()
    if target.parent != Path(".") or not target.name.startswith(".review-setup-"):
        raise ValueError("Unexpected saved setup location")
    return state / target


def gitlab_instances(rows):
    bases = set()
    for row in rows:
        if row["provider"] == "gitlab":
            base = project_base(row)
            if not row["url"].startswith(base + "/"):
                raise ValueError("Supply a new GitLab credential when changing GitLab instances")
            bases.add(base)
    return bases


def load_provider_key(selected):
    name = providers.KEYS[selected]
    if name + "_FILE" in os.environ:
        os.environ[name] = validate_token(read_secret(name))
        del os.environ[name + "_FILE"]


def effective_backend(saved):
    configured = saved.get("REVIEW_PROVIDER", "cursor")
    selected = providers.provider(os.getenv("REVIEW_PROVIDER", configured))
    default_mode = saved.get("REVIEW_AUTH_MODE", "api-key") if selected == configured else "api-key"
    return selected, auth_mode(selected, os.getenv("REVIEW_AUTH_MODE", default_mode))


def effective_projects(config):
    return json.loads(os.environ["REVIEW_PROJECTS"]) if "REVIEW_PROJECTS" in os.environ else config["projects"]


def apply_saved_setup(state, *, sync_only=False):
    """Snapshot one complete setup; explicitly supplied container settings win."""
    if not (state / "setup").is_symlink():
        if not sync_only and auth_mode(providers.provider()) == "api-key":
            load_provider_key(providers.provider())
        return
    with locked(state, fcntl.LOCK_SH):
        bundle = active_bundle(state)
        config = json.loads((bundle / "settings.json").read_text())
        values = {k: config["settings"][k] for k in SETTINGS if k in config["settings"]}
        values["REVIEW_PROJECTS"] = json.dumps(config["projects"])
        effective = effective_projects(config)
        forges = {row["provider"] for row in effective}
        selected, mode = effective_backend(values)
        if selected != values.get("REVIEW_PROVIDER", "cursor"):
            values.pop("REVIEW_AUTH_MODE", None)
            values.pop("REVIEW_LOGIN_PROFILE", None)
        if not sync_only and mode == "api-key":
            load_provider_key(selected)
        for name, location in config["secrets"].items():
            if name not in SECRETS:
                raise ValueError("Unknown credential in saved setup")
            if name in os.environ or name + "_FILE" in os.environ:
                continue
            if name in providers.KEYS.values() and (sync_only or mode == "login" or name != providers.KEYS[selected]):
                continue
            if name == KEY and "github" not in forges:
                continue
            if name in (TOKEN, *gitlab_tls.NAMES):
                if "gitlab" not in forges:
                    continue
                if gitlab_instances(effective) != gitlab_instances(config["projects"]):
                    raise ValueError("Supply a new GitLab credential when changing GitLab instances")
            path = bundle / location
            values[name] = path.read_text().strip()
        for k, v in values.items():
            os.environ.setdefault(k, v)


def saved_integer(state, name, default, upper=None):
    """Effective integer setting: container environment, then saved setup, then the default.

    Environment values keep the int() syntax the CLI has always accepted, such as
    "6_000" or "+6000"; the saved setting is written by the wizard as plain digits.
    """
    value = os.environ.get(name)
    saved = value is None and (state / "setup").is_symlink()
    if saved:
        with locked(state, fcntl.LOCK_SH):
            settings = json.loads((active_bundle(state) / "settings.json").read_text())["settings"]
        value = settings.get(name)
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip()
        try:
            value = int(value) if not saved or (value.isascii() and value.isdecimal()) else None
        except ValueError:
            value = None
    if type(value) is not int or value < 1 or (upper is not None and value > upper):
        raise ValueError(f"{name} must be " + (f"an integer from 1 to {upper}" if upper else "a positive integer"))
    return value


def max_input_tokens(state):
    """Effective prompt budget."""
    return saved_integer(state, "REVIEW_MAX_INPUT_TOKENS", DEFAULT_MAX_INPUT_TOKENS)


def visual_max_input_tokens(state):
    """Effective prompt budget of a visual review, which is its own."""
    return saved_integer(state, "REVIEW_VISUAL_MAX_INPUT_TOKENS", DEFAULT_VISUAL_MAX_INPUT_TOKENS)


def max_findings(state, *, tier=None, override=None):
    """Resolve the caps used by one request, or check every tier at poller startup."""
    requested = FINDINGS_SETTINGS if tier is None else (("mid", "max") if tier == "max" else (tier,))
    return {name: override if name == tier and override is not None else
            saved_integer(state, FINDINGS_SETTINGS[name], DEFAULT_MAX_FINDINGS, MAX_FINDINGS_LIMIT)
            for name in requested}


def ask(label, default=""):
    suffix = f" [{default}]" if default else ""
    return dialog.prompt(f"{label}{suffix}: ").strip() or default


def number(value):
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("Enter a positive integer")
    return value


def findings_cap(value):
    """A findings cap as the settings page saves it: plain digits within the bound."""
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= MAX_FINDINGS_LIMIT:
        raise ValueError(f"Enter an integer from 1 to {MAX_FINDINGS_LIMIT}")
    return str(int(value))


def nonnegative(value):
    return "0" if value == "0" else number(value)


def retry(reader):
    while True:
        try:
            return reader()
        except (ValueError, OSError) as err:
            dialog.notice(err)


def hidden(label, multiline):
    if dialog.browser.get():
        return dialog.browser.get().ask(label, secret=True, multiline=multiline)
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    quiet = list(saved)
    quiet[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSANOW, quiet)
    try:
        print(label, end=": ", file=sys.stderr, flush=True)
        lines = [dialog.terminal_input().strip()]
        if multiline and lines[0].startswith("-----BEGIN ") and "-----END " not in lines[0]:
            while not lines[-1].startswith("-----END "):
                lines.append(dialog.terminal_input().strip())
        return "\n".join(lines)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, saved)
        print(file=sys.stderr)


def credential_copy(name, bundle, old, location):
    path = old / location
    value = path.read_text().strip()
    value = validate_pem(value) if name == KEY else validate_token(value)
    if Path(location).is_absolute():
        return location
    private_file(bundle / SECRETS[name], value)
    return SECRETS[name]


def credential(name, label, bundle, old, previous_location):
    choices = ["paste", "import", "mount"]
    default = "import" if name == KEY else "paste"
    if previous_location:
        choices.append("keep")
        default = "keep"
    if dialog.browser.get():
        if previous_location:
            mode = ask(f"Keep existing {label}? (y/n)", "y").lower()
            if mode in ("y", "yes"):
                return credential_copy(name, bundle, old, previous_location)
        value = hidden(label, name == KEY)
        value = validate_pem(value) if name == KEY else validate_token(value)
        private_file(bundle / SECRETS[name], value)
        return SECRETS[name]
    mode = ask(f"{label} source ({'/'.join(choices)})", default).lower()
    if mode not in choices:
        raise ValueError("Choose one of " + ", ".join(choices))
    if mode == "paste":
        value = hidden("Paste " + label + " (hidden)", name == KEY)
    else:
        path = old / previous_location if mode == "keep" else Path(ask("Absolute file path inside the container"))
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Enter an absolute path to a readable file")
        if mode == "mount" and old and path.resolve().is_relative_to(old):
            raise ValueError("Use import to keep a copy of a setup-managed file")
        value = path.read_text(encoding="utf-8").strip()
    value = validate_pem(value) if name == KEY else validate_token(value)
    if mode == "mount" or (mode == "keep" and Path(previous_location).is_absolute()):
        return str(path)
    private_file(bundle / SECRETS[name], value)
    return SECRETS[name]


def project_rows(default_gitlab=None):
    rows = prompt_config(visibility=True, default_gitlab=default_gitlab)["projects"]
    return validate_rows(rows)


def validate_rows(rows):
    # Usernames are resolved to account IDs once the forge credentials are known.
    projects = parse_projects(accounts.shape(rows)).values()
    if any(urlsplit(p.url).netloc != "github.com" for p in projects if p.provider == "github"):
        raise ValueError("GitHub projects must be on github.com")
    if len(gitlab_instances(rows)) > 1:
        raise ValueError("Use one GitLab instance per container")
    return rows


def edit_projects(session, saved):
    rows = list(saved)
    while rows:
        session.message("Configured repositories: " + ", ".join(p["url"] for p in rows))
        action = session.choose("Repositories", ["continue", "add", "remove", "replace all"])
        if action == "continue":
            return rows
        if action == "replace all":
            break
        if action == "add":
            rows = retry(lambda: validate_rows(rows + project_rows(next(iter(gitlab_instances(rows)), None))))
        else:
            url = session.choose("Remove which repository?", [p["url"] for p in rows])
            rows = [p for p in rows if p["url"] != url]
    return retry(project_rows)


def pick_cursor_team(teams):
    options = [{"value": i, "label": f"{t['name']} ({i})"} for i, t in teams.items()]
    if dialog.browser.get():
        return dialog.browser.get().ask("Which Cursor team should pay for reviews?", choices=options)
    dialog.notice("Eligible Cursor teams: " + ", ".join(item["label"] for item in options))
    while True:
        selected = ask("Cursor billing team ID")
        if selected in teams:
            return selected
        dialog.notice("Enter one of the displayed team IDs.")


@contextmanager
def installer_lock(state):
    # One operator can perform remote credential creation at a time. Runtime
    # readers only take the short state lock during activation.
    with (state / ".configure.lock").open("a") as writer:
        try:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another installer is active on this volume.") from None
        yield


def commit(state, old, bundle):
    """Activate a finished bundle unless another save finished first."""
    link = state / (".setup-link-" + uuid.uuid4().hex)
    with locked(state, fcntl.LOCK_EX):
        if active_bundle(state) != old:
            raise ValueError("Another setup finished first; run setup again to edit its settings")
        link.symlink_to(bundle.name)
        try:
            os.replace(link, state / "setup")
        except BaseException:
            link.unlink(missing_ok=True)
            raise


def configure(state, *, force_login=False):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with installer_lock(state):
        configure_locked(state, force_login)


def configure_locked(state, force_login):
    session = dialog.browser.get()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(state, fcntl.LOCK_SH):
        old = active_bundle(state)
        prev = json.loads((old / "settings.json").read_text()) if old else {}
    if not dialog.browser.get():
        dialog.notice("Paste or import saves a private copy on the state volume. Mount uses a file you supply at each start.")
    if old:
        dialog.notice("Configured projects: " + ", ".join(p["url"] for p in prev["projects"]))
    rows = None
    if old and session:
        rows = edit_projects(session, prev["projects"])
    elif old and ask("Keep this project list? (y/n)", "y").lower() in ("y", "yes"):
        rows = prev["projects"]
    if rows is None:
        rows = retry(project_rows)
    projects = parse_projects(accounts.shape(rows))
    forges = {p.provider for p in projects.values()}
    previous = prev.get("settings", {})
    settings = {name: previous[name] for name in PRESERVED_SETTINGS if name in previous}

    def setting(name, label, validate, default=""):
        settings[name] = retry(lambda: validate(ask(label, previous.get(name, default))))

    setting("REVIEW_OPERATOR", "Responsible human (forge username or username:numeric-id)",
            lambda value: accounts.resolve_operator(value, forges))
    create = set()
    if session:
        for forge in sorted(forges):
            methods = ["create", "existing"]
            if old:
                methods.reverse()
            if session.choose(f"{'GitHub' if forge == 'github' else 'GitLab'} reviewer credentials", methods) == "create":
                create.add(forge)
    if "github" in forges and "github" not in create:
        setting(APP_ID, "GitHub App ID", number)
        setting(INSTALL_ID, "GitHub App installation ID", number)
    default_limit = str(DEFAULT_MAX_INPUT_TOKENS)
    defaults = {"REVIEW_POLL_SECONDS": "120", "REVIEW_MAX_INPUT_TOKENS": default_limit,
                "REVIEW_VISUAL_MAX_INPUT_TOKENS": str(DEFAULT_VISUAL_MAX_INPUT_TOKENS),
                "REVIEW_QUALGEN_INTERVAL_SECONDS": "86400"}
    if session and not old and ask(f"Use recommended settings (2-minute polling, {DEFAULT_MAX_INPUT_TOKENS:,} input tokens "
                                   f"per review and {DEFAULT_VISUAL_MAX_INPUT_TOKENS:,} per visual review, daily history "
                                   "refresh)? (y/n)", "y").lower() in ("y", "yes"):
        settings.update(defaults)
    else:
        setting("REVIEW_POLL_SECONDS", "Seconds between polls", number, "120")
        setting("REVIEW_MAX_INPUT_TOKENS", "Maximum estimated input tokens per review", number, default_limit)
        setting("REVIEW_VISUAL_MAX_INPUT_TOKENS", "Maximum estimated input tokens per visual review", number,
                str(DEFAULT_VISUAL_MAX_INPUT_TOKENS))
        setting("REVIEW_QUALGEN_INTERVAL_SECONDS", "Seconds between Qualgen history refreshes (0 disables)",
                nonnegative, "86400")
    backend = retry(lambda: providers.provider(ask("Review provider (cursor/claude/codex)", previous.get("REVIEW_PROVIDER", "cursor"))))
    settings["REVIEW_PROVIDER"] = backend
    model = ask(providers.LABELS[backend] + " model (auto for default)",
                previous.get("REVIEW_MODEL", "auto") if backend == previous.get("REVIEW_PROVIDER", "cursor") else "auto")
    if model != "auto":
        settings["REVIEW_MODEL"] = model
    default_mode = "login"
    if old and backend == previous.get("REVIEW_PROVIDER", "cursor"):
        default_mode = previous.get("REVIEW_AUTH_MODE", "api-key")
    mode = retry(lambda: auth_mode(backend, ask("Authentication method (login/api-key)", default_mode)))
    settings["REVIEW_AUTH_MODE"] = mode
    bundle = Path(tempfile.mkdtemp(dir=state, prefix=".review-setup-"))
    committed = False
    pending = []
    staged_login = None
    try:
        if mode == "login":
            current_profile = previous.get("REVIEW_LOGIN_PROFILE", "") if backend == previous.get("REVIEW_PROVIDER") else ""
            directory = login_directory(state, current_profile)
            # Isolate account changes and Cursor team switches from in-flight reviews.
            if force_login or backend == "cursor" or not old or backend != previous.get("REVIEW_PROVIDER"):
                profile = uuid.uuid4().hex
                staged_login = login_directory(state, profile)
                staged_login.mkdir(mode=0o700, parents=True)
                if not force_login and (directory / backend).is_dir():
                    shutil.copytree(directory / backend, staged_login / backend, symlinks=True)
                directory = staged_login
                settings["REVIEW_LOGIN_PROFILE"] = profile
            elif current_profile:
                settings["REVIEW_LOGIN_PROFILE"] = current_profile
            if session:
                with session.cv:
                    session.view.update(status="working", label="Continue sign-in in the attached installer terminal.")
            ensure_session(backend, directory, interactive=True)
        required = {}
        if mode == "api-key":
            required[providers.KEYS[backend]] = providers.LABELS[backend] + " API key"
        if "github" in forges and "github" not in create:
            required[KEY] = "GitHub App PEM"
        if "gitlab" in forges and "gitlab" not in create:
            required[TOKEN] = "GitLab reviewer token"
        secrets = {}
        for name, label in required.items():
            prev_path = prev.get("secrets", {}).get(name)
            if name == TOKEN and gitlab_instances(rows) != gitlab_instances(prev.get("projects", [])):
                prev_path = None
            secrets[name] = retry(lambda: credential(name, label, bundle, old, prev_path))
        if backend == "cursor":
            key = (bundle / secrets[CURSOR]).read_text().strip() if mode == "api-key" else None
            with tempfile.TemporaryDirectory(prefix="review-team-check-") as temp:
                team_home = Path(temp) if key else Path(login_env(backend, directory)["HOME"])
                chosen = providers.select_team(key, os.getenv("REVIEW_CURSOR_TEAM_ID", ""), team_home, pick_cursor_team)
            if chosen:
                settings["REVIEW_CURSOR_TEAM_ID"] = chosen
        if "github" in create:
            gh_projects = [p for p in projects.values() if p.provider == "github"]
            ids, path = provision.github(session, state, gh_projects, bundle / SECRETS[KEY])
            settings.update(ids)
            pending.append(path)
            secrets[KEY] = SECRETS[KEY]
        if "gitlab" in forges:
            for name, filename in gitlab_tls.NAMES.items():
                if name in os.environ or name + "_FILE" in os.environ:
                    private_file(bundle / filename, gitlab_tls.validate(name, read_secret(name)))
                    secrets[name] = filename
                    continue
                previous_path = prev.get("secrets", {}).get(name) if gitlab_instances(rows) == gitlab_instances(prev.get("projects", [])) else None
                label = "private CA certificate bundle" if name == gitlab_tls.CA else "certificate revocation list (CRL)"
                options = (["keep"] if previous_path else []) + ["skip", "import"]
                method = session.choose("GitLab " + label, options) if session else ask(
                    f"GitLab {label} ({'/'.join(options)})", options[0])
                if method == "skip":
                    continue
                if method == "keep" and previous_path:
                    value = (old / previous_path).read_text()
                elif method == "import":
                    value = (session.ask("Paste or upload PEM " + label, multiline=True, upload=True)
                             if session else Path(ask("PEM file path inside container")).read_text())
                else:
                    raise ValueError("Unknown GitLab TLS setting")
                private_file(bundle / filename, gitlab_tls.validate(name, value))
                secrets[name] = filename
            gitlab_tls.context(bundle)
        if "gitlab" in create:
            gl_projects = [p for p in projects.values() if p.provider == "gitlab"]
            token, path = provision.gitlab(session, state, gl_projects, verify=gitlab_tls.context(bundle))
            private_file(bundle / SECRETS[TOKEN], token)
            pending.append(path)
            secrets[TOKEN] = SECRETS[TOKEN]
        if "gitlab" in forges:
            # GitLab username lookups need the reviewer token, which only exists now.
            token = accounts.saved_token() or (bundle / secrets[TOKEN]).read_text().strip()
            verify = gitlab_tls.context(bundle)
            rows = accounts.resolve(rows, token=token, verify=verify, notice=print)
            settings["REVIEW_OPERATOR"] = accounts.resolve_operator(
                settings["REVIEW_OPERATOR"], forges, base=next(iter(gitlab_instances(rows))), token=token, verify=verify)
        if accounts.pending(rows):
            raise ValueError("GitLab usernames need the reviewer token; enter numeric IDs for: " + ", ".join(accounts.pending(rows)))
        projects = parse_projects(rows)
        private_file(bundle / "settings.json", json.dumps(
            {"projects": rows, "settings": settings, "secrets": secrets}, indent=2))
        commit(state, old, bundle)
        committed = True
        try:
            for path in pending:
                path.unlink(missing_ok=True)
            if old:
                shutil.rmtree(old)
            retire_profiles(state, {settings.get("REVIEW_LOGIN_PROFILE"), previous.get("REVIEW_LOGIN_PROFILE")})
        except OSError:
            print("Settings were saved. Some previous installer files could not be removed.", file=sys.stderr)
    finally:
        if not committed:
            shutil.rmtree(bundle)
            if staged_login:
                shutil.rmtree(staged_login)
    print(f"Saved configuration for {len(rows)} project(s) in {state / 'setup'}.")


def check_login(state, *, interactive=False, force=False):
    # The login subprocess needs only the selected provider and the mode, never forge secrets.
    settings = {}
    if (state / "setup").is_symlink():
        with locked(state, fcntl.LOCK_SH):
            settings = json.loads((active_bundle(state) / "settings.json").read_text())["settings"]
    saved = settings.get("REVIEW_PROVIDER", "cursor")
    backend = providers.provider(os.getenv("REVIEW_PROVIDER", saved))
    default = settings.get("REVIEW_AUTH_MODE", "api-key") if backend == saved else "api-key"
    if auth_mode(backend, os.getenv("REVIEW_AUTH_MODE", default)) == "login":
        profile = settings.get("REVIEW_LOGIN_PROFILE", "") if saved == backend else ""
        directory = login_directory(state, os.getenv("REVIEW_LOGIN_PROFILE", profile))
        ensure_session(backend, directory, interactive=interactive, force=force)


def login_main(argv=None):
    parser = argparse.ArgumentParser(description="Sign in with the configured provider CLI")
    parser.add_argument("--state-dir", type=Path, default=state_dir())
    parser.add_argument("--force", action="store_true", help="Sign in again even if the current session is valid")
    args = parser.parse_args(argv)
    try:
        check_login(args.state_dir.resolve(), interactive=True, force=args.force)
    except (ValueError, OSError, subprocess.TimeoutExpired) as err:
        parser.exit(2, f"Provider login: {err}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Configure projects, credentials and review costs interactively")
    parser.add_argument("--state-dir", type=Path, default=state_dir())
    parser.add_argument("--reauth", action="store_true", help="Run a fresh provider sign-in")
    parser.add_argument("--web", action="store_true", help="Open the local browser wizard")
    args = parser.parse_args(argv)
    state = args.state_dir.resolve()
    try:
        if args.web:
            from .web_setup import serve
            result = serve(state, force_login=args.reauth)
            if result:
                return result
        else:
            if not sys.stdin.isatty():
                parser.exit(2, "Setup needs an interactive terminal. Use docker run -it or docker exec -it.\n")
            print("Type /quit at a setup prompt to return to your shell.", flush=True)
            configure(state, force_login=args.reauth)
    except dialog.QuitSetup:
        return dialog.QUIT_MANAGER if os.getenv("REVIEW_SETUP_MANAGER") == "1" else 0
    except (KeyboardInterrupt, EOFError):
        parser.exit(1, "\nSetup cancelled.\n")
    except (ValueError, OSError, subprocess.TimeoutExpired) as err:
        parser.exit(2, f"Setup failed: {err}\n")
