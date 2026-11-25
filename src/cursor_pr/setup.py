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

from . import dialog, providers
from .login import auth_mode, ensure_session, login_directory
from .config import parse_projects, prompt_config
from .credentials import APP_ID, INSTALL_ID, KEY, TOKEN, private_file, read_secret, validate_operator, validate_pem, validate_token

CURSOR = "CURSOR_API_KEY"
SECRETS = {CURSOR: "cursor.key", "ANTHROPIC_API_KEY": "claude.key", "CODEX_API_KEY": "codex.key", KEY: "github.pem", TOKEN: "gitlab.token"}
SETTINGS = ("REVIEW_OPERATOR", APP_ID, INSTALL_ID, "REVIEW_MODEL", "REVIEW_MAX_INPUT_TOKENS",
            "REVIEW_POLL_SECONDS", "REVIEW_QUALGEN_INTERVAL_SECONDS", "REVIEW_PROVIDER", "REVIEW_CURSOR_TEAM_ID", "REVIEW_AUTH_MODE")


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


def gitlab_hosts(rows):
    return {urlsplit(row["url"]).netloc for row in rows if row["provider"] == "gitlab"}


def load_provider_key(selected):
    name = providers.KEYS[selected]
    if name + "_FILE" in os.environ:
        os.environ[name] = validate_token(read_secret(name))
        del os.environ[name + "_FILE"]


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
        effective = json.loads(os.getenv("REVIEW_PROJECTS", values["REVIEW_PROJECTS"]))
        forges = {row["provider"] for row in effective}
        selected = providers.provider(os.environ.get("REVIEW_PROVIDER", values.get("REVIEW_PROVIDER", "cursor")))
        if selected != values.get("REVIEW_PROVIDER", "cursor"):
            values.pop("REVIEW_AUTH_MODE", None)
        mode = auth_mode(selected, os.getenv("REVIEW_AUTH_MODE", values.get("REVIEW_AUTH_MODE", "api-key")))
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
            if name == TOKEN:
                if "gitlab" not in forges:
                    continue
                if gitlab_hosts(effective) != gitlab_hosts(config["projects"]):
                    raise ValueError("Supply a new GitLab credential when changing GitLab instances")
            path = bundle / location
            values[name] = path.read_text().strip()
        for k, v in values.items():
            os.environ.setdefault(k, v)


def ask(label, default=""):
    suffix = f" [{default}]" if default else ""
    return dialog.prompt(f"{label}{suffix}: ").strip() or default


def number(value):
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("Enter a positive integer")
    return value


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
        lines = [input().strip()]
        if multiline and lines[0].startswith("-----BEGIN ") and "-----END " not in lines[0]:
            while not lines[-1].startswith("-----END "):
                lines.append(input().strip())
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


def project_rows():
    rows = prompt_config(visibility=True)["projects"]
    projects = parse_projects(rows).values()
    if any(urlsplit(p.url).netloc != "github.com" for p in projects if p.provider == "github"):
        raise ValueError("GitHub projects must be on github.com")
    if len({urlsplit(p.url).netloc for p in projects if p.provider == "gitlab"}) > 1:
        raise ValueError("Use one GitLab instance per container")
    return rows


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


def configure(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(state, fcntl.LOCK_SH):
        old = active_bundle(state)
        prev = json.loads((old / "settings.json").read_text()) if old else {}
    if not dialog.browser.get():
        dialog.notice("Paste or import saves a private copy on the state volume. Mount uses a file you supply at each start.")
    if old:
        dialog.notice("Configured projects: " + ", ".join(p["url"] for p in prev["projects"]))
    rows = prev.get("projects") if old and ask("Keep this project list? (y/n)", "y").lower() in ("y","yes") else None
    if rows is None:
        rows = retry(project_rows)
    projects = parse_projects(rows)
    forges = {p.provider for p in projects.values()}
    previous = prev.get("settings", {})
    settings = {}

    def setting(name, label, validate, default=""):
        settings[name] = retry(lambda: validate(ask(label, previous.get(name, default))))

    setting("REVIEW_OPERATOR", "Responsible human (forge login:numeric-id)", validate_operator)
    if "github" in forges:
        setting(APP_ID, "GitHub App ID", number)
        setting(INSTALL_ID, "GitHub App installation ID", number)
    defaults = {"REVIEW_POLL_SECONDS": "120", "REVIEW_MAX_INPUT_TOKENS": "6000",
                "REVIEW_QUALGEN_INTERVAL_SECONDS": "86400"}
    if dialog.browser.get() and ask("Use recommended settings (2-minute polling, 6,000 input tokens, daily history refresh)? (y/n)", "y").lower() in ("y", "yes"):
        settings.update(defaults)
    else:
        setting("REVIEW_POLL_SECONDS", "Seconds between polls", number, "120")
        setting("REVIEW_MAX_INPUT_TOKENS", "Maximum estimated input tokens per review", number, "6000")
        setting("REVIEW_QUALGEN_INTERVAL_SECONDS", "Seconds between Qualgen history refreshes (0 disables)",
                lambda value: "0" if value == "0" else number(value), "86400")
    backend = retry(lambda: providers.provider(ask("Review provider (cursor/claude/codex)", previous.get("REVIEW_PROVIDER", "cursor"))))
    settings["REVIEW_PROVIDER"] = backend
    model = ask(providers.LABELS[backend] + " model (auto for default)",
                previous.get("REVIEW_MODEL", "auto") if backend == previous.get("REVIEW_PROVIDER", "cursor") else "auto")
    if model != "auto":
        settings["REVIEW_MODEL"] = model
    mode = "api-key"
    if backend in ("claude", "codex"):
        default = "login"
        if backend == previous.get("REVIEW_PROVIDER"):
            default = previous.get("REVIEW_AUTH_MODE", "api-key")
        mode = retry(lambda: auth_mode(backend, ask("Authentication method (login/api-key)", default)))
        settings["REVIEW_AUTH_MODE"] = mode
    bundle = Path(tempfile.mkdtemp(dir=state, prefix=".review-setup-"))
    link = state / (".setup-link-" + uuid.uuid4().hex)
    committed = False
    try:
        required = {}
        if mode == "api-key":
            required[providers.KEYS[backend]] = providers.LABELS[backend] + " API key"
        if "github" in forges:
            required[KEY] = "GitHub App PEM"
        if "gitlab" in forges:
            required[TOKEN] = "GitLab reviewer token"
        secrets = {}
        for name, label in required.items():
            prev_path = prev.get("secrets", {}).get(name)
            if name == TOKEN and gitlab_hosts(rows) != gitlab_hosts(prev.get("projects", [])):
                prev_path = None
            secrets[name] = retry(lambda: credential(name, label, bundle, old, prev_path))
        if backend == "cursor":
            key = (bundle / secrets[CURSOR]).read_text().strip()
            with tempfile.TemporaryDirectory(prefix="review-team-check-") as temp:
                chosen = providers.select_team(key, os.getenv("REVIEW_CURSOR_TEAM_ID", ""), Path(temp), pick_cursor_team)
            if chosen:
                settings["REVIEW_CURSOR_TEAM_ID"] = chosen
        private_file(bundle / "settings.json", json.dumps(
            {"projects": rows, "settings": settings, "secrets": secrets}, indent=2))
        with locked(state, fcntl.LOCK_EX):
            if active_bundle(state) != old:
                raise ValueError("Another setup finished first; run setup again to edit its settings")
            link.symlink_to(bundle.name)
            os.replace(link, state / "setup")
            committed = True
            if old:
                shutil.rmtree(old)
    finally:
        link.unlink(missing_ok=True)
        if not committed:
            shutil.rmtree(bundle)
    print(f"Saved configuration for {len(rows)} project(s) in {state / 'setup'}. "
          "Start or restart the reviewer using this state volume.")


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
        ensure_session(backend, login_directory(state), interactive=interactive, force=force)


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
    parser.add_argument("--web", action="store_true", help="Open the local browser wizard")
    args = parser.parse_args(argv)
    state = args.state_dir.resolve()
    try:
        if args.web:
            from .web_setup import serve
            result = serve(state)
            if result:
                return result
        else:
            if not sys.stdin.isatty():
                parser.exit(2, "Setup needs an interactive terminal. Use docker run -it or docker exec -it.\n")
            configure(state)
        check_login(state, interactive=True)
    except (KeyboardInterrupt, EOFError):
        parser.exit(1, "\nSetup cancelled.\n")
    except (ValueError, OSError, subprocess.TimeoutExpired) as err:
        parser.exit(2, f"Setup failed: {err}\n")
