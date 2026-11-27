"""Discover review requests in the current open forge conversations."""
from datetime import datetime
from itertools import batched
import logging
import os
import time
from urllib.parse import quote

import httpx

from .gitlab_tls import client_options
from .source import ReviewError, reviewer_token, run_forge

log = logging.getLogger(__name__)


def open_changes(project):
    result = run_forge("open", project, timeout=120)
    numbers = result.get("numbers")
    if (not isinstance(numbers, list)
            or any(type(number) is not int or number < 1 for number in numbers)
            or len(set(numbers)) != len(numbers)):
        raise ReviewError("Invalid open change list")
    complete = result.get("capped") is False
    if not complete:
        # Keep polling the returned changes, and surface the coverage limit.
        log.warning("Open change list capped for %s", project.key)
    return numbers, complete


def comment_threads(project, numbers):
    result = run_forge("comments", project, numbers=numbers, timeout=120)
    threads = {}
    for row in result["items"]:
        number = row["number"]
        if (type(number) is not int or number not in numbers or number in threads
                or not isinstance(row.get("comments"), list)):
            raise ReviewError("Mismatched change comment item")
        threads[number] = row["comments"]
    complete = not result.get("partial") and set(threads) == set(numbers)
    if not complete:
        log.warning("Incomplete comment read for %s; missing threads will be polled again", project.key)
    return threads, complete


def signature(notes):
    # What tells a conversation with new comments from an unchanged one. The times are only compared.
    return (len(notes), max((note["createdAt"] for note in notes
                             if isinstance(note.get("createdAt"), str) and len(note["createdAt"]) <= 64), default=""))


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
    # GitLab's conversation list has an immutable account ID but no actor type.
    return note.get("authorType") in (None, "", "User")


def public_gitlab_request(project, number, note):
    # The list adapter omits note visibility and rounds GitLab timestamps to
    # seconds. Check both on this single candidate before a public response.
    base = os.environ.get("GITLAB_API_BASE", "").rstrip("/")
    if base != project.url.removesuffix("/" + project.slug) + "/api/v4":
        raise ReviewError("GITLAB_API_BASE must match the project's HTTPS instance /api/v4")
    token = reviewer_token("gitlab", project.slug, project.checkout)
    url = f"{base}/projects/{quote(project.slug, safe='')}/merge_requests/{number}/notes/{note['databaseId']}"
    response = httpx.get(url, headers={"PRIVATE-TOKEN": token}, follow_redirects=False, timeout=15, **client_options())
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


def poll_project(project, jobs, since, ack=None, idle=None):
    """Read new requests; return how many open changes were read and how many were left for a later cycle.

    idle is (poll interval, cap). A change whose complete read found nothing new waits twice as long
    before its next read, up to the cap. New comments or a request return it to the poll interval.
    """
    numbers, listing_complete = open_changes(project)
    waiting = (jobs.waiting(project.key, numbers, time.time(), idle[1], listing_complete)
               if idle is not None else set())
    due = [number for number in numbers if number not in waiting]
    read = 0
    for numbers in batched(due, 16):
        try:
            threads, complete = comment_threads(project, numbers)
        except Exception:
            log.exception("Comment read failed for %s changes %s", project.key, numbers)
            continue
        read += len(threads)
        active = set()
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
                        active.add(number)  # Look again next cycle, as before there was a schedule.
                        continue
                    if not public:
                        jobs.enqueue(project.key, number, comment, "ignored")
                        continue
                jobs.enqueue(project.key, number, comment)
                active.add(number)
                if ack is not None:
                    ack.queued(project, number, comment)
        # An incomplete read says nothing about the changes it asked for, so it never advances the schedule.
        if idle is not None and complete:
            now = time.time()
            for number, notes in threads.items():
                jobs.observed(project.key, number, *signature(notes), number in active, now, *idle)
    return read, len(waiting)
