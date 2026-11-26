"""First-start settings for polling projects."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

from .dialog import notice, prompt
from .scanners import ci_config

# Prompt budget when neither REVIEW_MAX_INPUT_TOKENS nor the saved setup supplies one.
DEFAULT_MAX_INPUT_TOKENS = 6000
# A visual review explains the whole change, so its budget is its own. Its instructions are
# far shorter than the review rubric and each hunk is shown once, so twice the budget reads
# several times more of the change.
DEFAULT_VISUAL_MAX_INPUT_TOKENS = 12000


@dataclass(frozen=True)
class Project:
    provider: str
    url: str
    slug: str
    checkout: Path
    requesters: frozenset[int]
    visibility: str = "private"
    go_analysis: str = ""
    managed: bool = False
    scanner_ci: dict = field(default_factory=dict)

    @property
    def key(self):
        return self.provider + "/" + self.url.removeprefix("https://")

    def change_url(self, number):
        segment = "/pull/" if self.provider == "github" else "/-/merge_requests/"
        return self.url + segment + str(number)


def https_url(value):
    url = urlsplit(value)
    if (any(c.isspace() or not c.isprintable() for c in value)
            or url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise ValueError("Expected an HTTPS URL without credentials, query or fragment")
    if url.hostname in (".", "..") or not re.fullmatch(r"[a-zA-Z0-9.:-]+", url.netloc):
        raise ValueError("Invalid project host")
    return value.rstrip("/")


def gitlab_base(value):
    url = https_url(value)
    if url.endswith("/api/v4"):
        url = url[:-7]
    parts = urlsplit(url)
    if not parts.hostname or parts.port == 0 or ":" in parts.hostname:
        raise ValueError("Expected a GitLab hostname and optional HTTPS port")
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", segment) or segment in (".", "..")
           for segment in parts.path.split("/")[1:]):
        raise ValueError("Invalid GitLab installation path")
    return url


def project_base(row):
    url = https_url(row["url"])
    base = row.get("gitlab_base") or os.getenv("REVIEW_GITLAB_BASE_URL") or os.getenv("GITLAB_API_BASE")
    return gitlab_base(base or ("https://" + urlsplit(url).netloc))


def project_from(row):
    url = https_url(row["url"])
    slug = urlsplit(url).path.strip("/")
    if row["provider"] == "gitlab":
        prefix = project_base(row) + "/"
        if not url.startswith(prefix):
            raise ValueError("GitLab base URL must match the project URL")
        slug = url[len(prefix):]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", slug):
        raise ValueError("Expected an owner/project slug")
    if any(part in (".", "..") for part in slug.split("/")):
        raise ValueError("Invalid project slug")
    if row["provider"] not in ("github", "gitlab"):
        raise ValueError("Provider must be github or gitlab")
    ids = row["requester_ids"]
    if not isinstance(ids, list) or not ids or any(type(i) is not int or i <= 0 for i in ids):
        raise ValueError("requester_ids must list positive numeric IDs of human reviewers")
    managed = row.get("managed", False)
    if type(managed) is not bool:
        raise ValueError("managed must be true or false")
    checkout = (Path("/repos") / row["provider"] / urlsplit(url).netloc / urlsplit(url).path.strip("/") / "current"
                if managed else Path(row["checkout"]))
    if not checkout.is_absolute() or (not managed and not checkout.is_dir()):
        raise ValueError("checkout must be an existing absolute directory on the poller")
    visibility = row.get("visibility", "private")
    if visibility not in ("private", "internal", "public"):
        raise ValueError("visibility must be private, internal or public")
    producer = row.get("go_analysis", "")
    if not isinstance(producer, str) or len(producer) > 255 or (
            producer and (not producer.isprintable() or producer != producer.strip())):
        raise ValueError("go_analysis must name a CI producer")
    if row["provider"] == "github" and producer and not re.fullmatch(r"[\w.-]+\.ya?ml|[1-9][0-9]*", producer):
        raise ValueError("go_analysis must be a GitHub workflow filename or numeric ID")
    settings = row.get("scanner_ci", {})
    ci_config(settings, row["provider"])
    return Project(row["provider"], url, slug, checkout, frozenset(ids), visibility, producer, managed, settings)


def prompt_config(*, visibility=False, default_gitlab=None):
    from .accounts import lookup, requester_id
    rows = []
    while True:
        provider = prompt("Forge (github/gitlab): ").strip().lower()
        base = ""
        if provider == "gitlab":
            default = default_gitlab or os.getenv("REVIEW_GITLAB_BASE_URL") or os.getenv("GITLAB_API_BASE") or "https://gitlab.com"
            base = gitlab_base(prompt(f"GitLab base URL [{default}]: ").strip() or default)
            default_gitlab = base
            repo = prompt(f"Project path on {base} (group/project or full HTTPS URL): ").strip()
            url = repo if repo.startswith("https://") else base + "/" + repo
        else:
            url = prompt("Full HTTPS project URL: ").strip()
        checkout = prompt("Checkout path inside container (Enter to sync automatically): ").strip()
        while True:
            try:
                ids = [lookup(provider, requester_id(x), base=base or None, notice=notice)
                       for x in prompt("Human requester usernames or numeric IDs (comma-separated): ").split(",")]
            except ValueError as err:
                notice(err)
            else:
                break
        rows.append(dict(provider=provider, url=url, checkout=checkout, requester_ids=ids))
        if base:
            rows[-1]["gitlab_base"] = base
        if not checkout:
            rows[-1]["managed"] = True
        if visibility or "REVIEW_OPERATOR" in os.environ:
            rows[-1]["visibility"] = prompt("Project visibility (private/internal/public) [private]: ").strip() or "private"
        label = "GitHub Actions workflow filename or ID" if provider == "github" else "GitLab CI job name"
        producer = prompt(f"Trusted Go analysis {label} (blank to skip): ").strip()
        if producer:
            rows[-1]["go_analysis"] = producer
        if prompt("Add another project? [y/N] ").strip().lower() not in ("y", "yes"):
            return {"projects": rows}


def parse_projects(rows):
    projects = {}
    for row in rows:
        project = project_from(row)
        if any(p.slug.casefold() == project.slug.casefold() for p in projects.values()):
            raise ValueError("Use an unambiguous owner/project address")
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
        from . import accounts
        rows = accounts.resolve(prompt_config()["projects"], token=accounts.saved_token(), notice=notice)
        if accounts.pending(rows):
            raise ValueError("Looking up GitLab usernames needs REVIEW_GITLAB_TOKEN; enter numeric IDs instead")
        save = True
    else:
        raise ValueError("Run cursor-review-setup with a TTY or set REVIEW_PROJECTS")
    projects = parse_projects(rows)
    if save:
        with tempfile.NamedTemporaryFile(mode="w", dir=state, delete=False) as temp:
            json.dump({"projects": [{k: row[k] for k in ("provider", "url", "checkout", "requester_ids", "visibility", "go_analysis", "managed", "scanner_ci", "gitlab_base") if k in row}
                                    for row in rows]}, temp)
        os.replace(temp.name, path)
    return projects
