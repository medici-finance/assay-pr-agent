"""First-start settings for project-specific webhook endpoints."""
from dataclasses import dataclass, field
from getpass import getpass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Project:
    provider: str
    url: str
    slug: str
    checkout: Path
    requesters: frozenset[int]
    secret: bytes = field(repr=False)

    @property
    def key(self):
        return self.provider + "/" + self.url.removeprefix("https://")

    def change_url(self, number):
        segment = "/pull/" if self.provider == "github" else "/-/merge_requests/"
        return self.url + segment + str(number)


def https_url(value):
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise ValueError("Expected an HTTPS URL without credentials, query or fragment")
    return value.rstrip("/")


def project_from(row):
    url = https_url(row["url"])
    slug = urlsplit(url).path.strip("/")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", slug):
        raise ValueError("deskread and deskpost require an owner/project slug")
    if row["provider"] not in ("github", "gitlab"):
        raise ValueError("Provider must be github or gitlab")
    ids = row["requester_ids"]
    if not isinstance(ids, list) or not ids or any(type(i) is not int or i <= 0 for i in ids):
        raise ValueError("requester_ids must list positive numeric IDs of human reviewers")
    if "secret_env" in row and not re.fullmatch(r"REVIEW_SECRET_[A-Z0-9_]+", row["secret_env"]):
        raise ValueError("Use a REVIEW_SECRET_ environment variable for each webhook secret")
    secret = os.environ.get(row["secret_env"], "") if "secret_env" in row else row.get("secret", "")
    if not secret or not secret.isascii() or not secret.isprintable() or any(c.isspace() for c in secret):
        raise ValueError("Set a nonempty printable webhook secret without whitespace")
    checkout = Path(row["checkout"])
    if not checkout.is_absolute() or not checkout.is_dir():
        raise ValueError("checkout must be an existing absolute directory on the receiver")
    return Project(row["provider"], url, slug, checkout, frozenset(ids), secret.encode())


def prompt_config():
    public = input("Public HTTPS receiver URL: ").strip()
    rows = []
    while True:
        provider = input("Forge (github/gitlab): ").strip().lower()
        url = input("Full HTTPS project URL: ").strip()
        checkout = input("Trusted checkout path inside this service: ").strip()
        ids = [int(x.strip()) for x in input("Human requester numeric IDs (comma-separated): ").split(",")]
        secret = getpass("Webhook secret (enter the same secret in the forge): ")
        rows.append(dict(provider=provider, url=url, checkout=checkout, requester_ids=ids, secret=secret))
        if input("Add another project? [y/N] ").strip().lower() not in ("y", "yes"):
            return {"public_url": public, "projects": rows}


def load_config(state):
    path = state / "hooks.json"
    fresh = False
    if "REVIEW_PROJECTS" in os.environ:
        settings = {"projects": json.loads(os.environ["REVIEW_PROJECTS"]),
                    "public_url": os.environ.get("REVIEW_PUBLIC_URL", "")}
    elif path.exists():
        settings = json.loads(path.read_text())
    elif sys.stdin.isatty():
        settings = prompt_config()
        fresh = True
    else:
        raise ValueError("Set REVIEW_PROJECTS and REVIEW_PUBLIC_URL or start interactively with a TTY")
    public = https_url(os.environ.get("REVIEW_PUBLIC_URL", settings["public_url"]))
    projects = {}
    for row in settings["projects"]:
        project = project_from(row)
        if project.key in projects:
            raise ValueError("Duplicate project endpoint")
        projects[project.key] = project
    if not projects:
        raise ValueError("Configure at least one project")
    if fresh:
        with tempfile.NamedTemporaryFile(mode="w", dir=state, delete=False) as temp:
            json.dump(settings, temp)
        os.replace(temp.name, path)
    return public, projects
