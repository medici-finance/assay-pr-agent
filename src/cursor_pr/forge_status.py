"""Report a reviewed forge change on its head: a GitHub check run or a GitLab commit status."""
from contextlib import contextmanager
import logging
import os
import re
from urllib.parse import quote, urlsplit

import httpx

from .gitlab_tls import client_options
from .scanners import fence, safe_path
from .source import ReviewError, reviewer_token

log = logging.getLogger(__name__)
NAME = "assay-pr-agent/review"
# GitHub accepts 50 annotations per request and 64 KB per message; GitLab descriptions are 255 characters.
ANNOTATIONS = 50
DESCRIPTION = 255
COMPLETE = ("reviewed", "empty")
SENTENCES = {
    "reviewed": "The review completed.",
    "empty": "The review completed. The change has no files to review.",
    "partial": "The review covered only part of the change.",
    "over-budget": "The model was skipped because the estimated prompt is over the budget.",
    "model-unavailable": "The model could not complete the review.",
}


def change(address, url):
    """(provider, slug, GitLab API base) for a reviewed change. The link comes from the forge, so pin it."""
    slug, _, number = address.rpartition("#")
    site = urlsplit(url)
    base = os.getenv("GITLAB_API_BASE", "").rstrip("/")
    instance = urlsplit(base)
    # A GitLab instance may be served under a path, such as https://host/gitlab/group/project.
    root = instance.path.removesuffix("/api/v4")
    path = site.path[len(root):] if "/-/merge_requests/" in site.path and root and site.path.startswith(root + "/") else site.path
    match = re.fullmatch(r"/([\w.-]+/[\w.-]+)/(pull|-/merge_requests)/([1-9][0-9]*)", path)
    provider = "github" if match and match[2] == "pull" else "gitlab"
    if (not match or match[1].casefold() != slug.casefold() or match[3] != number
            or not url.isprintable() or site.scheme != "https" or site.username is not None
            or site.query or site.fragment or (provider == "github" and site.netloc != "github.com")
            or (provider == "gitlab" and not (base.endswith("/api/v4") and site.netloc == instance.netloc
                                              and site.path.startswith(instance.path.removesuffix("/api/v4") + "/")))):
        raise ReviewError("The change URL does not match the reviewed address and forge")
    return provider, slug, base


def tally(count, thing):
    return f"{count} {thing}" + ("" if count == 1 else "s")


def facts(value):
    """(title, detail, head) from fixed sentences and counts: nothing a change author or the model wrote."""
    state, head = value["state"], value["head"]
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise ReviewError("The reviewed head is not a commit SHA")
    checks = [*value["checks"], *value.get("static_checks", [])]
    counts = [tally(len(value["findings"]), "model finding"),
              tally(sum(len(c["findings"]) for c in checks), "static and scanner finding"),
              tally(sum(c["state"] != "complete" for c in checks), "check that could not run"),
              tally(len(value["omitted"]), "path outside the review")]
    return (f"{state}: {counts[0]}",
            SENTENCES.get(state, "See the change for the result.") + " " + " · ".join(counts), head)


def annotations(value):
    rows = []
    for f in value["findings"]:
        line, end, title, message = f["line"], f.get("end_line"), f["title"], f["message"]
        if not (safe_path(f["path"]) and type(line) is int and line > 0
                and isinstance(title, str) and title.strip() and isinstance(message, str) and message.strip()):
            continue
        rows.append({"path": f["path"], "start_line": line, "end_line": end if type(end) is int and end >= line else line,
                     "annotation_level": "warning", "message": f"{fence(title, 255)}. {fence(message, 700)}"})
    # GitHub renders annotations as Markdown, so each untrusted value sits in its own code span.
    return rows[:ANNOTATIONS]


def github(value, url, title, detail, head):
    return {"name": NAME, "head_sha": head, "status": "completed",
            "conclusion": "success" if value["state"] in COMPLETE else "failure", "details_url": url,
            "output": {"title": title, "summary": f"Assay PR Agent review of {head}\n\n{detail}",
                       "annotations": annotations(value)}}


def gitlab(value, url, title, detail):
    body = {"state": "success" if value["state"] in COMPLETE else "failed", "name": NAME,
            "description": f"{title}. {detail}"[:DESCRIPTION]}
    if len(url) <= DESCRIPTION:
        body["target_url"] = url
    return body


@contextmanager
def connection(address, url, checkout):
    """(client, provider, slug, GitLab API base, headers) as the reviewer. Never follows a redirect."""
    provider, slug, base = change(address, url)
    token = reviewer_token(provider, slug, checkout)
    options = client_options() if provider == "gitlab" else {}
    mounts = {"https://" + urlsplit(url).netloc: httpx.HTTPTransport(**options)} if options else {}
    # A redirect would carry the credential to another host, so it is an error and is not followed.
    with httpx.Client(timeout=15, follow_redirects=False, mounts=mounts) as client:
        yield (client, provider, slug, base,
               {"PRIVATE-TOKEN": token} if provider == "gitlab" else {"Authorization": "Bearer " + token})


def post(address, value, checkout):
    """Create the status on the reviewed head. Raises on any refusal; never logs the credential."""
    url = value["source"]
    title, detail, head = facts(value)
    with connection(address, url, checkout) as (client, provider, slug, base, headers):
        if provider == "github":
            response = client.post(f"https://api.github.com/repos/{slug}/check-runs", headers=headers,
                                   json=github(value, url, title, detail, head))
        else:
            response = client.post(f"{base}/projects/{quote(slug, safe='')}/statuses/{head}", headers=headers,
                                   json=gitlab(value, url, title, detail))
        response.raise_for_status()


def outcome(args, value):
    """'posted', 'failed' or 'skipped'. A refusal never changes the review's own result or exit code."""
    if not (args.change and not args.estimate
            and (getattr(args, "forge_status", False) or os.getenv("REVIEW_FORGE_STATUS") == "1")):
        return "skipped"
    try:
        post(args.change, value, args.repo)
    # Optional output must not fail a review, whatever the transport or the forge answer was.
    except Exception as error:
        reason = str(error) if isinstance(error, ReviewError) else (
            f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else type(error).__name__)
        log.warning("Forge status for %s was not posted (%s)", args.change, reason)
        return "failed"
    return "posted"
