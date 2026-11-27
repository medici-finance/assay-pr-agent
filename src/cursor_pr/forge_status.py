"""Report a reviewed forge change on its head: a GitHub check run or a GitLab commit status."""
from contextlib import contextmanager
import logging
import os
import re
from urllib.parse import quote, urlsplit

import httpx

from .gitlab_tls import client_options
from .scanners import fence, safe_path
from .source import ReviewError, check_publication, reviewer_token

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
    if (not re.fullmatch(r"[\w.-]+/[\w.-]+", slug) or not re.fullmatch(r"[1-9][0-9]*", number)
            or not url.isprintable() or site.scheme != "https" or site.username is not None
            or site.query or site.fragment or any(part in (".", "..") for part in slug.split("/"))):
        raise ReviewError("The change URL does not match the reviewed address and forge")
    if site.netloc == "github.com" and site.path == f"/{slug}/pull/{number}":
        return "github", slug, ""
    base = os.getenv("GITLAB_API_BASE", "").rstrip("/")
    instance = urlsplit(base)
    prefix = instance.path.removesuffix("/api/v4")
    if (base.endswith("/api/v4") and instance.scheme == "https" and instance.netloc == site.netloc
            and instance.username is None and not (instance.query or instance.fragment)
            and site.path == f"{prefix}/{slug}/-/merge_requests/{number}"):
        return "gitlab", slug, base
    raise ReviewError("The change URL does not match the reviewed address and forge")


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
    provider, slug, base = change(address, url)
    token = reviewer_token(provider, slug, checkout)
    if provider == "github":
        headers, options = {"Authorization": "Bearer " + token}, {}
    else:
        headers, options = {"PRIVATE-TOKEN": token}, client_options()
    mounts = {"https://" + urlsplit(url).netloc: httpx.HTTPTransport(**options)} if options else {}
    # Redirects could carry the reviewer credential to another host.
    with httpx.Client(timeout=15, follow_redirects=False, mounts=mounts) as client:
        yield client, provider, slug, base, headers


def gitlab_source_ref(client, headers, base, slug, number, head):
    # The authorized status target is this configured project. Do not write to a fork's project.
    response = client.get(f"{base}/projects/{quote(slug, safe='')}/merge_requests/{number}", headers=headers)
    response.raise_for_status()
    change = response.json()
    if not isinstance(change, dict) or change.get("sha") != head:
        raise ReviewError("The GitLab merge request moved on since the review")
    source, target, ref = (change.get(name) for name in ("source_project_id", "target_project_id", "source_branch"))
    if type(source) is not int or type(target) is not int or source < 1 or source != target:
        raise ReviewError("The GitLab status source project is outside the configured publication target")
    if not isinstance(ref, str) or not ref or not ref.isprintable():
        raise ReviewError("GitLab did not identify the reviewed source branch")
    # A branch pipeline and a merge request pipeline can both exist for this commit, and a status
    # sent with the branch alone can land on the one the merge request does not show. Name the
    # merge request's own pipeline, with the ref GitLab keeps for it, when it is one of this
    # project's and ran this commit. Otherwise the branch is all there is to go by.
    pipeline = change.get("head_pipeline")
    if isinstance(pipeline, dict):
        number, own = pipeline.get("id"), pipeline.get("ref")
        if (type(number) is int and number > 0 and pipeline.get("sha") == head and pipeline.get("project_id") == target
                and isinstance(own, str) and own and own.isprintable()):
            return {"ref": own, "pipeline_id": number}
    return {"ref": ref}


def post(address, value, project):
    """Apply the comment publication policy before creating a status on the reviewed head."""
    number = int(address.rsplit("#", 1)[1])
    url = value["source"]
    provider, slug, base = change(address, url)
    if not project or project.provider != provider or project.slug != slug or project.change_url(number) != url:
        raise ReviewError("The status target is not a configured review project")
    title, detail, head = facts(value)
    if provider == "github":
        endpoint = f"https://api.github.com/repos/{slug}/check-runs"
        body = github(value, url, title, detail, head)
        output = body["output"]
        checked = [output["title"], output["summary"]]
        for row in output["annotations"]:
            checked.extend((fence(row["path"]), row["message"]))
    else:
        endpoint = f"{base}/projects/{quote(slug, safe='')}/statuses/{head}"
        body = gitlab(value, url, title, detail)
        checked = [body["description"]]
    check_publication(project, number, head, "\n\n".join(checked))
    with connection(address, url, project.checkout) as (client, _, _, _, headers):
        if provider == "gitlab":
            body.update(gitlab_source_ref(client, headers, base, slug, number, head))
        client.post(endpoint, headers=headers, json=body).raise_for_status()


def outcome(args, value):
    """'posted', 'failed' or 'skipped'. A refusal never changes the review's own result or exit code."""
    if not (args.change and not args.estimate
            and (getattr(args, "forge_status", False) or os.getenv("REVIEW_FORGE_STATUS") == "1")):
        return "skipped"
    try:
        post(args.change, value, getattr(args, "forge_project", None))
    # Optional output must not fail a review, whatever the transport or the forge answer was.
    except Exception as error:
        reason = str(error) if isinstance(error, ReviewError) else (
            f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else type(error).__name__)
        log.warning("Forge status for %s was not posted (%s)", args.change, reason)
        return "failed"
    return "posted"
