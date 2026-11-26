"""Prepare instance-bound reviewer credentials from the agent’s settings."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

from . import gitlab_tls

KEY = "REVIEW_GITHUB_PRIVATE_KEY"
TOKEN = "REVIEW_GITLAB_TOKEN"
APP_ID = "REVIEW_GITHUB_APP_ID"
INSTALL_ID = "REVIEW_GITHUB_INSTALLATION_ID"
SECRET_INPUTS = (KEY, KEY + "_FILE", TOKEN, TOKEN + "_FILE", *(k for n in gitlab_tls.NAMES for k in (n, n + "_FILE")))
SETUP_INPUTS = (*SECRET_INPUTS, APP_ID, INSTALL_ID, "REVIEW_OPERATOR")


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


def validate_pem(value):
    pem = value.replace("\\n", "\n") + "\n"
    if not pem.startswith(("-----BEGIN RSA PRIVATE KEY-----\n", "-----BEGIN PRIVATE KEY-----\n")):
        raise ValueError(f"{KEY} must contain an unencrypted RSA PEM")
    check = subprocess.run(["openssl", "rsa", "-check", "-noout", "-passin", "pass:"],
                           input=pem.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={"PATH": os.defpath})
    if check.returncode:
        raise ValueError(f"{KEY} must contain an unencrypted RSA PEM")
    return pem.rstrip()


def validate_token(value):
    if not value or not value.isascii() or not value.isprintable() or any(c.isspace() for c in value):
        raise ValueError("Expected a single printable token without spaces")
    return value


def private_file(path, value):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tmp:
        tmp.write(value + "\n")
    os.replace(tmp.name, path)


def validate_operator(operator):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*:[1-9][0-9]*", operator):
        raise ValueError("Set REVIEW_OPERATOR to the responsible human's forge login:numeric-id")
    return operator


@contextmanager
def forge_credentials(projects):
    # Keep runtime copies separate from read-only secret projections and the state volume.
    ram = Path("/dev/shm")
    with tempfile.TemporaryDirectory(prefix="review-credentials-", dir=ram if ram.is_dir() else None) as directory, ExitStack() as stack:
        creds = Path(directory)
        configured = list(projects.values())
        providers = {p.provider for p in configured}
        updates = {"REVIEW_FORGE_DIR": directory}
        if "github" in providers:
            if any(urlsplit(p.url).netloc != "github.com" for p in configured if p.provider == "github"):
                raise ValueError("GitHub App credentials require github.com")
            updates["REVIEW_FORGE_APP_ID"] = positive_id(APP_ID)
            updates["REVIEW_FORGE_INSTALL_ID"] = positive_id(INSTALL_ID)
            private_file(creds / "github.pem", validate_pem(read_secret(KEY)))
        if "gitlab" in providers:
            bases = {p.url.removesuffix("/" + p.slug) + "/api/v4" for p in configured if p.provider == "gitlab"}
            base = os.getenv("GITLAB_API_BASE", "").rstrip("/")
            if len(bases) != 1 or (base and base not in bases):
                raise ValueError("Configure one GitLab instance per container; GITLAB_API_BASE must match it")
            updates["GITLAB_API_BASE"] = bases.pop()
            if os.getenv("REVIEW_GITLAB_BASE_URL"):
                from .config import gitlab_base
                if gitlab_base(os.environ["REVIEW_GITLAB_BASE_URL"]) + "/api/v4" != updates["GITLAB_API_BASE"]:
                    raise ValueError("REVIEW_GITLAB_BASE_URL must match the selected projects")
            for name, filename in gitlab_tls.NAMES.items():
                if name in os.environ or name + "_FILE" in os.environ:
                    private_file(creds / filename, gitlab_tls.validate(name, read_secret(name)))
            updates.update(stack.enter_context(gitlab_tls.transport(updates["GITLAB_API_BASE"], creds)))
            private_file(creds / "gitlab.token", validate_token(read_secret(TOKEN)))
        operator = os.getenv("REVIEW_OPERATOR")
        if operator:
            updates["REVIEW_FORGE_OPERATOR"] = validate_operator(operator).split(":")[0].lower()
        updates["REVIEW_PRIVATE_REPOSITORIES"] = ",".join(p.slug for p in configured if p.visibility == "private")
        previous = {key: os.environ.get(key) for key in (*updates, *SECRET_INPUTS)}
        for key in SECRET_INPUTS:
            os.environ.pop(key, None)
        os.environ.update(updates)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
