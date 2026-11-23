"""First-start settings for polling projects."""
from dataclasses import dataclass
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
    visibility: str = "private"

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
    checkout = Path(row["checkout"])
    if not checkout.is_absolute() or not checkout.is_dir():
        raise ValueError("checkout must be an existing absolute directory on the poller")
    visibility = row.get("visibility", "private")
    if visibility not in ("private", "internal", "public"):
        raise ValueError("visibility must be private, internal or public")
    return Project(row["provider"], url, slug, checkout, frozenset(ids), visibility)


def prompt_config(*, visibility=False):
    rows = []
    while True:
        provider = input("Forge (github/gitlab): ").strip().lower()
        url = input("Full HTTPS project URL: ").strip()
        checkout = input("Trusted checkout path inside this service: ").strip()
        ids = [int(x.strip()) for x in input("Human requester numeric IDs (comma-separated): ").split(",")]
        rows.append(dict(provider=provider, url=url, checkout=checkout, requester_ids=ids))
        if visibility or "REVIEW_OPERATOR" in os.environ:
            rows[-1]["visibility"] = input("Project visibility (private/internal/public) [private]: ").strip() or "private"
        if input("Add another project? [y/N] ").strip().lower() not in ("y", "yes"):
            return {"projects": rows}


def parse_projects(rows):
    projects = {}
    for row in rows:
        project = project_from(row)
        if any(p.slug.casefold() == project.slug.casefold() for p in projects.values()):
            raise ValueError("Assay requires an unambiguous owner/project address")
        projects[project.key] = project
    if not projects:
        raise ValueError("Configure at least one project")
    return projects


def load_config(state):
    path = state / "projects.json"
    legacy = state / "hooks.json"
    save = False
    if "REVIEW_PROJECTS" in os.environ:
        rows = json.loads(os.environ["REVIEW_PROJECTS"])
    elif path.exists() or legacy.exists():
        rows = json.loads((path if path.exists() else legacy).read_text())["projects"]
        save = not path.exists()
    elif sys.stdin.isatty():
        rows = prompt_config()["projects"]
        save = True
    else:
        raise ValueError("Run cursor-review-setup with a TTY or set REVIEW_PROJECTS")
    projects = parse_projects(rows)
    if save:
        with tempfile.NamedTemporaryFile(mode="w", dir=state, delete=False) as temp:
            json.dump({"projects": [{k: row[k] for k in ("provider", "url", "checkout", "requester_ids", "visibility") if k in row}
                                    for row in rows]}, temp)
        os.replace(temp.name, path)
    return projects
