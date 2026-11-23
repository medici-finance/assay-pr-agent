"""Interactive configuration of a persistent reviewer state volume."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import termios
from urllib.parse import urlsplit
import uuid

from .config import parse_projects, prompt_config
from .credentials import APP_ID, INSTALL_ID, KEY, TOKEN, private_file, read_secret, validate_operator, validate_pem, validate_token

CURSOR = "CURSOR_API_KEY"
SECRETS = {CURSOR: "cursor.key", KEY: "github.pem", TOKEN: "gitlab.token"}
SETTINGS = ("REVIEW_OPERATOR", APP_ID, INSTALL_ID, "REVIEW_MODEL", "REVIEW_MAX_INPUT_TOKENS",
            "REVIEW_POLL_SECONDS", "REVIEW_QUALGEN_INTERVAL_SECONDS")


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


def apply_saved_setup(state):
    """Snapshot one complete setup; explicitly supplied container settings win."""
    if CURSOR + "_FILE" in os.environ:
        os.environ[CURSOR] = validate_token(read_secret(CURSOR))
        os.environ.pop(CURSOR + "_FILE")
    if not (state / "setup").is_symlink():
        return
    with locked(state, fcntl.LOCK_SH):
        bundle = active_bundle(state)
        config = json.loads((bundle / "settings.json").read_text())
        values = {k: config["settings"][k] for k in SETTINGS if k in config["settings"]}
        values["REVIEW_PROJECTS"] = json.dumps(config["projects"])
        effective = json.loads(os.getenv("REVIEW_PROJECTS", values["REVIEW_PROJECTS"]))
        providers = {row["provider"] for row in effective}
        for name, location in config["secrets"].items():
            if name not in SECRETS:
                raise ValueError("Unknown credential in saved setup")
            if name in os.environ or name + "_FILE" in os.environ:
                continue
            if name == KEY and "github" not in providers:
                continue
            if name == TOKEN:
                if "gitlab" not in providers:
                    continue
                if gitlab_hosts(effective) != gitlab_hosts(config["projects"]):
                    raise ValueError("Supply a new GitLab credential when changing GitLab instances")
            path = bundle / location
            values[name] = path.read_text().strip()
        for k, v in values.items():
            os.environ.setdefault(k, v)


def ask(label, default=""):
    suffix = f" [{default}]" if default else ""
    return input(f"{label}{suffix}: ").strip() or default


def number(value):
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("Enter a positive integer")
    return value


def retry(reader):
    while True:
        try:
            return reader()
        except (ValueError, OSError) as err:
            print(err, file=sys.stderr)


def hidden(label, multiline):
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


def credential(name, label, bundle, old, previous_location):
    choices = ["paste", "import", "mount"]
    default = "import" if name == KEY else "paste"
    if previous_location:
        choices.append("keep")
        default = "keep"
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


def configure(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(state, fcntl.LOCK_SH):
        old = active_bundle(state)
        prev = json.loads((old / "settings.json").read_text()) if old else {}
    print("Configure the reviewer. Paste or import saves a private copy on the state volume. "
          "Mount saves a path to a file you will provide at each start.")
    if old:
        print("Configured projects:", ", ".join(p["url"] for p in prev["projects"]))
    rows = prev.get("projects") if old and ask("Keep this project list? (y/n)", "y").lower() in ("y","yes") else None
    if rows is None:
        rows = retry(project_rows)
    projects = parse_projects(rows)
    providers = {p.provider for p in projects.values()}
    previous = prev.get("settings", {})
    settings = {}

    def setting(name, label, validate, default=""):
        settings[name] = retry(lambda: validate(ask(label, previous.get(name, default))))

    setting("REVIEW_OPERATOR", "Responsible human (forge login:numeric-id)", validate_operator)
    if "github" in providers:
        setting(APP_ID, "GitHub App ID", number)
        setting(INSTALL_ID, "GitHub App installation ID", number)
    setting("REVIEW_POLL_SECONDS", "Seconds between polls", number, "120")
    setting("REVIEW_MAX_INPUT_TOKENS", "Maximum estimated input tokens per review", number, "6000")
    setting("REVIEW_QUALGEN_INTERVAL_SECONDS", "Seconds between Qualgen history refreshes (0 disables)",
            lambda value: "0" if value == "0" else number(value), "86400")
    model = ask("Cursor model (auto for Cursor's default)", previous.get("REVIEW_MODEL", "auto"))
    if model != "auto":
        settings["REVIEW_MODEL"] = model
    bundle = Path(tempfile.mkdtemp(dir=state, prefix=".review-setup-"))
    link = state / (".setup-link-" + uuid.uuid4().hex)
    committed = False
    try:
        required = {CURSOR: "Cursor API key"}
        if "github" in providers:
            required[KEY] = "GitHub App PEM"
        if "gitlab" in providers:
            required[TOKEN] = "GitLab reviewer token"
        secrets = {}
        for name, label in required.items():
            prev_path = prev.get("secrets", {}).get(name)
            if name == TOKEN and gitlab_hosts(rows) != gitlab_hosts(prev.get("projects", [])):
                prev_path = None
            secrets[name] = retry(lambda: credential(name, label, bundle, old, prev_path))
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Configure projects, credentials and review costs interactively")
    parser.add_argument("--state-dir", type=Path, default=state_dir())
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        parser.exit(2, "Setup needs an interactive terminal. Use docker run -it or docker exec -it.\n")
    try:
        configure(args.state_dir.resolve())
    except (KeyboardInterrupt, EOFError):
        parser.exit(1, "\nSetup cancelled.\n")
    except (ValueError, OSError) as err:
        parser.exit(2, f"Setup failed: {err}\n")
