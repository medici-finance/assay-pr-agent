"""Discover review requests with Assay's released monitor and typed change reads."""
from datetime import datetime
import hashlib
from itertools import batched
import json
import logging
import os
from urllib.parse import quote, urlsplit

import httpx

from .source import ReviewError, reviewer_token, run_desk

log = logging.getLogger(__name__)


def open_changes(project, state):
    # deskmonitor's documented baseline includes the whole open population. Its
    # event lines alone miss ordinary conversation comments on an unchanged head.
    directory = state / "monitor" / hashlib.sha256(project.key.encode()).hexdigest()
    run_desk(["deskmonitor", "pr", project.slug], cwd=project.checkout,
        env={**os.environ, "PR_MONITOR_STATE_DIR": str(directory)}, timeout=120)
    baseline = directory / (project.slug.replace("/", "__") + ".state")
    numbers = []
    for line in baseline.read_text().splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[0].isascii() or not fields[0].isdigit() or int(fields[0]) < 1:
            raise ReviewError("Invalid deskmonitor PR baseline")
        numbers.append(int(fields[0]))
    return numbers


def comment_threads(project, numbers):
    args = ["deskread", "comments"]
    for number in numbers:
        args.extend(["--change", f"{project.slug}#{number}"])
    envelope = json.loads(run_desk(args, cwd=project.checkout, timeout=120))
    if envelope.get("schema") != 1 or envelope.get("kind") != "comments":
        raise ReviewError("Invalid deskread comments envelope")
    threads = {}
    for row in envelope["items"] or []:
        number = row["number"]
        if (type(number) is not int or number not in numbers or number in threads
                or row.get("repo") != project.slug or not isinstance(row.get("comments"), list)):
            raise ReviewError("Mismatched deskread comments item")
        threads[number] = row["comments"]
    if envelope.get("partial") or set(threads) != set(numbers):
        log.warning("Incomplete comment read for %s; missing threads will be polled again", project.key)
    return threads


def is_request(project, number, note, since):
    author, comment = note.get("authorId"), note.get("databaseId")
    if (type(author) is not int or author not in project.requesters
            or type(comment) is not int or comment < 1 or not isinstance(note.get("body"), str)
            or note["body"].strip() != "/review" or not note.get("createdAt")
            or note["createdAt"] != note.get("updatedAt")):
        return False
    try:
        created = datetime.fromisoformat(note["createdAt"])
        if created.tzinfo is None or created < since:
            return False
    except (TypeError, ValueError):
        return False
    if project.provider == "github":
        return (note.get("authorType") == "User"
                and note.get("url") == f"{project.change_url(number)}#issuecomment-{comment}")
    # GitLab's deskread comment has an immutable account ID but no actor type.
    return note.get("authorType") in (None, "", "User")


def public_gitlab_request(project, number, note):
    # deskread v1.0.91 omits note visibility and rounds GitLab timestamps to
    # seconds. Check both on this single candidate before a public response.
    base = os.environ.get("GITLAB_API_BASE", "").rstrip("/")
    if base != "https://" + urlsplit(project.url).netloc + "/api/v4":
        raise ReviewError("GITLAB_API_BASE must match the project's HTTPS instance /api/v4")
    token = reviewer_token("gitlab", project.slug, project.checkout)
    url = f"{base}/projects/{quote(project.slug, safe='')}/merge_requests/{number}/notes/{note['databaseId']}"
    response = httpx.get(url, headers={"PRIVATE-TOKEN": token}, follow_redirects=False, timeout=15)
    if response.status_code == 404:
        return False
    response.raise_for_status()
    latest = response.json()
    return (latest.get("internal") is False and not latest.get("confidential")
            and latest.get("system") is False and latest.get("noteable_type") == "MergeRequest"
            and latest.get("noteable_iid") == number and latest.get("id") == note["databaseId"]
            and latest.get("author", {}).get("id") == note["authorId"]
            and not latest["author"].get("bot") and latest.get("body", "").strip() == "/review"
            and bool(latest.get("created_at")) and latest["created_at"] == latest.get("updated_at"))


def poll_project(project, state, jobs, since):
    for numbers in batched(open_changes(project, state), 16):
        try:
            threads = comment_threads(project, numbers)
        except Exception:
            log.exception("Comment read failed for %s changes %s", project.key, numbers)
            continue
        for number, notes in threads.items():
            for note in notes:
                if not is_request(project, number, note, since):
                    continue
                comment = note["databaseId"]
                if jobs.recorded(project.key, number, comment):
                    continue
                if project.provider == "gitlab":
                    try:
                        public = public_gitlab_request(project, number, note)
                    except (OSError, ValueError, ReviewError, httpx.HTTPError):
                        log.exception("Could not verify GitLab note %s!%d/%d", project.slug, number, comment)
                        continue
                    if not public:
                        jobs.enqueue(project.key, number, comment, "ignored")
                        continue
                jobs.enqueue(project.key, number, comment)
