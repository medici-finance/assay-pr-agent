"""Prepare Assay reviewer credentials from container settings before polling."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit


KEY = "REVIEW_GITHUB_PRIVATE_KEY"
TOKEN = "REVIEW_GITLAB_TOKEN"
APP_ID = "REVIEW_GITHUB_APP_ID"
INSTALL_ID = "REVIEW_GITHUB_INSTALLATION_ID"
SECRET_INPUTS = (KEY, KEY + "_FILE", TOKEN, TOKEN + "_FILE")
SETUP_INPUTS = (*SECRET_INPUTS, APP_ID, INSTALL_ID, "REVIEW_OPERATOR")


def standalone_requested():
    return any(k in os.environ for k in SETUP_INPUTS)


def read_secret(name):
    file_key = name + "_FILE"
    if name in os.environ and file_key in os.environ:
        raise ValueError(f"Set only one of {name} and {file_key}")
    if file_key in os.environ:
        path = Path(os.environ[file_key])
        if not path.is_absolute():
            raise ValueError(f"{file_key} must be an absolute path inside the container")
        try:
            value = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ValueError(f"Cannot read {file_key} as UTF-8") from None
    else:
        value = os.getenv(name, "")
    if not value.strip():
        raise ValueError(f"Set {name} or {file_key}")
    return value.strip()


def positive_id(name):
    value = os.getenv(name, "")
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError(f"{name} must be a positive numeric ID")
    return value


def private_file(path, value):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tmp:
        tmp.write(value + "\n")
    os.replace(tmp.name, path)


def write_roster(home, projects):
    operator = os.getenv("REVIEW_OPERATOR", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*:[1-9][0-9]*", operator):
        raise ValueError("Set REVIEW_OPERATOR to the responsible human's forge login:numeric-id")
    login = operator.split(":")[0]
    config = home / ".config/assay"
    config.mkdir(mode=0o700, parents=True, exist_ok=True)
    repos = ",".join(p.slug + ":" + p.visibility for p in projects)
    forges = ",".join(p.slug + "=" + p.provider for p in projects)
    private_file(config / "roster.env", f"ASSAY_BLESS_LOGIN={operator}\n"
                 f"ASSAY_HUMAN_LOGIN_MAP=review-operator:{login}\n"
                 f"ASSAY_ALLOWED_REPOS={repos}\nASSAY_REPO_FORGES={forges}")


@contextmanager
def forge_credentials(projects, state):
    if not standalone_requested():
        yield
        return
    # /dev/shm is container-local tmpfs in Docker. Secret volume files may be
    # read-only projections; Assay's credential directory also holds mutable caches.
    ram = Path("/dev/shm")
    with tempfile.TemporaryDirectory(prefix="review-credentials-", dir=ram if ram.is_dir() else None) as directory:
        creds = Path(directory)
        configured = list(projects.values())
        providers = {p.provider for p in configured}
        updates = {"ASSAY_CONFIG_HOME": directory, "DESK_LOOP": "pr-review-desk"}
        if "github" in providers:
            if any(urlsplit(p.url).netloc != "github.com" for p in configured if p.provider == "github"):
                raise ValueError("The pinned Assay GitHub App backend requires github.com")
            updates["REVIEWER_APP_ID"] = positive_id(APP_ID)
            updates["REVIEWER_INSTALL_ID"] = positive_id(INSTALL_ID)
            pem = read_secret(KEY).replace("\\n", "\n") + "\n"
            if not pem.startswith(("-----BEGIN RSA PRIVATE KEY-----\n", "-----BEGIN PRIVATE KEY-----\n")):
                raise ValueError(f"{KEY} must contain an unencrypted RSA PEM")
            check = subprocess.run(["openssl", "rsa", "-check", "-noout", "-passin", "pass:"],
                                   input=pem.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env={"PATH": os.defpath})
            if check.returncode:
                raise ValueError(f"{KEY} must contain an unencrypted RSA PEM")
            private_file(creds / "reviewer-app.pem", pem.rstrip())
            updates["REVIEWER_APP"] = "reviewer-app"
            updates["REVIEWER_PEM"] = str(creds / "reviewer-app.pem")
        if "gitlab" in providers:
            bases = {"https://" + urlsplit(p.url).netloc + "/api/v4" for p in configured if p.provider == "gitlab"}
            base = os.getenv("GITLAB_API_BASE", "").rstrip("/")
            if len(bases) != 1 or (base and base not in bases):
                raise ValueError("Configure one GitLab instance per container; GITLAB_API_BASE must match it")
            updates["GITLAB_API_BASE"] = bases.pop()
            pat = read_secret(TOKEN)
            if not pat.isascii() or not pat.isprintable() or any(c.isspace() for c in pat):
                raise ValueError(f"{TOKEN} must be a single printable token")
            private_file(creds / "gitlab-reviewer.token", pat)
        if not (Path.home() / ".config/assay/roster.env").exists():
            # Keep audit records and Assay's write limits across container restarts.
            home = state / "forge-home"
            write_roster(home, configured)
            updates["REVIEW_ASSAY_HOME"] = str(home)
        previous = {k: os.environ.get(k) for k in updates}
        for k in SECRET_INPUTS:
            os.environ.pop(k, None)
        os.environ.update(updates)
        try:
            yield
        finally:
            for k, value in previous.items():
                if value is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = value
