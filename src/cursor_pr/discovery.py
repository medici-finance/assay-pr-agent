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
# A request is a comment holding exactly one of these commands; the value is the review tier.
COMMANDS = {"/review": "regular", "/review-mid": "mid", "/review-max": "max"}


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


def comment_threads(project, numbers, *, verb="comments"):
    result = run_forge(verb, project, numbers=numbers, timeout=120)
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
            or note["body"].strip() not in COMMANDS or not note.get("createdAt")
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


def gitlab_notes(project, number):
    base = os.environ.get("GITLAB_API_BASE", "").rstrip("/")
    if base != project.url.removesuffix("/" + project.slug) + "/api/v4":
        raise ReviewError("GITLAB_API_BASE must match the project's HTTPS instance /api/v4")
    token = reviewer_token("gitlab", project.slug, project.checkout)
    return f"{base}/projects/{quote(project.slug, safe='')}/merge_requests/{number}/notes", token


def visible_gitlab_note(note, number):
    return (note.get("internal") is False and not note.get("confidential")
            and note.get("system") is False and note.get("noteable_type") == "MergeRequest"
            and note.get("noteable_iid") == number)


def context_comments(project, number):
    """Read visible context comments from authorized review requesters."""
    if project.provider == "github":
        notes = comment_threads(project, [number], verb="context-comments")[0].get(number)
        if notes is None:
            return None
        return [note for note in notes if type(note.get("authorId")) is int
                and note["authorId"] in project.requesters and note.get("authorType") == "User"
                and type(note.get("databaseId")) is int and note["databaseId"] > 0
                and note.get("url") == f"{project.change_url(number)}#issuecomment-{note['databaseId']}"]
    url, token = gitlab_notes(project, number)
    response = httpx.get(url, headers={"PRIVATE-TOKEN": token}, follow_redirects=False, timeout=15,
                         params={"per_page": 20, "activity_filter": "only_comments",
                                 "sort": "desc", "order_by": "created_at"},
                         **client_options())
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ReviewError("Invalid GitLab note list")
    return [row for row in reversed(rows) if isinstance(row, dict) and visible_gitlab_note(row, number)
            and isinstance(author := row.get("author"), dict) and type(author.get("id")) is int
            and author["id"] in project.requesters and not author.get("bot")]


def public_gitlab_request(project, number, note):
    # The list adapter omits note visibility and rounds GitLab timestamps to
    # seconds. Check both on this single candidate before a public response.
    notes_url, token = gitlab_notes(project, number)
    url = f"{notes_url}/{note['databaseId']}"
    response = httpx.get(url, headers={"PRIVATE-TOKEN": token}, follow_redirects=False, timeout=15, **client_options())
    if response.status_code == 404:
        return False
    response.raise_for_status()
    latest = response.json()
    return (visible_gitlab_note(latest, number) and latest.get("id") == note["databaseId"]
            and latest.get("author", {}).get("id") == note["authorId"]
            and not latest["author"].get("bot") and latest.get("body", "").strip() == note["body"].strip()
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
                tier = COMMANDS[note["body"].strip()]
                if tier != "regular":
                    jobs.set_tier(project.key, number, comment, tier)
                jobs.enqueue(project.key, number, comment, acknowledge=ack is not None)
                active.add(number)
                if ack is not None:
                    ack.queued(project, number, comment)
        # An incomplete read says nothing about the changes it asked for, so it never advances the schedule.
        if idle is not None and complete:
            now = time.time()
            for number, notes in threads.items():
                jobs.observed(project.key, number, *signature(notes), number in active, now, *idle)
    return read, len(waiting)
