"""Post model findings as inline comments on the reviewed head, outside the forge helper's publication checks."""
import hashlib
import json
import logging
import os
import re
from urllib.parse import quote

import httpx

from . import forge_status, providers
from .scanners import fence, safe_path
from .source import ReviewError, run_forge

log = logging.getLogger(__name__)
PREFIX = "<!-- assay-pr-agent:inline:%s:"
MARK = PREFIX + "%s -->"
# Caps for the two code spans; the summary comment carries each finding in full.
CAPS = (255, 2000)
PAGES = 20
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class Stale(ReviewError):
    """The change moved on since the review, so its lines may no longer be the same code."""


def comments(value):
    """([path, line, body]) per model finding that can be placed, and how many could not be."""
    rows, left = [], 0
    for f in value["findings"]:
        line = f["line"]
        if not (safe_path(f["path"]) and type(line) is int and line > 0 and isinstance(f["title"], str) and f["title"].strip()
                and isinstance(f["message"], str) and f["message"].strip()):
            left += 1
            continue
        rows.append([f["path"], line, "\n\n".join(fence(part, cap) for part, cap in zip((f["title"], f["message"]), CAPS))])
    return rows, left


def marker(head, text):
    """Hidden text naming the head and a digest of the finding text, so a repeat run can recognise its own post."""
    return MARK % (head, hashlib.sha256(json.dumps(text, ensure_ascii=True).encode()).hexdigest())


def carried(head, text):
    """The marker that closes one body the reviewer identity wrote, if it does.

    Finding text is quoted and single-line, so a marker can only be the closing line we wrote.
    Reading that line alone keeps a marker planted inside a finding from counting.
    """
    lines = str(text).rstrip().rsplit("\n", 2)
    if len(lines) != 3 or lines[1] or not re.fullmatch(re.escape(PREFIX % head) + r"[0-9a-f]{64} -->", lines[2]):
        return set()
    return {lines[2]}


def thread(project, number):
    """The change's comments through the forge helper, complete or not at all."""
    data = run_forge("comments", project, numbers=[int(number)])
    items = data.get("items") or []
    if (data.get("partial") or len(items) != 1 or items[0].get("number") != int(number)
            or not isinstance(items[0].get("comments"), list)):
        raise ReviewError("Incomplete or mismatched change comment read")
    return items[0]["comments"]


def review_comments(client, headers, slug, number):
    """Every review comment on the pull request, read to the end; an unread page could hide an earlier post."""
    found, page = [], 1
    while True:
        response = client.get(f"https://api.github.com/repos/{slug}/pulls/{number}/comments",
                              params={"page": page, "per_page": 100}, headers=headers)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ReviewError("Invalid pull request review comment list")
        found += [r for r in rows if isinstance(r, dict)]
        if len(rows) < 100:
            return found
        if page >= PAGES:
            raise ReviewError("Too many review comments to check for earlier inline comments")
        page += 1


def reviewer(project, number, value):
    """The account that posts this tool's summaries on GitHub: a bot, the author of the newest one."""
    # An installation token cannot read its own identity, so the summary just posted stands in for it.
    # Only a bot can match, and lines the publication adds may sit around the summary's first line.
    first = f"{providers.LABELS[value.get('provider', 'cursor')]} review of {value['head']}"
    ours = [c for c in thread(project, number) if isinstance(c, dict) and c.get("authorType") == "Bot"
            and isinstance(c.get("body"), str) and first in c["body"].split("\n")]
    if not ours or any(type(c.get(k)) is not int for c in ours for k in ("authorId", "databaseId")):
        raise ReviewError("Could not identify the reviewer identity on the change")
    return max(ours, key=lambda c: c["databaseId"])["authorId"]


def account(client, headers, base):
    """The reviewer's own user on GitLab: a project access token acts as its bot user."""
    response = client.get(base + "/user", headers=headers)
    response.raise_for_status()
    me = response.json().get("id") if isinstance(response.json(), dict) else None
    if type(me) is not int:
        raise ReviewError("Could not identify the reviewer identity on the change")
    return me


def unmoved(project, number, head):
    # Read last, right before the first write: a comment on a moved head lands on the wrong line.
    if run_forge("change", project, numbers=[int(number)]).get("headSha") != head:
        raise Stale("the change moved on since the review")


def github(client, headers, project, slug, number, rows, me, head):
    """(posted, refused, already present) as one review on the reviewed head."""
    # Each comment closes with its own marker, as on GitLab, so a later run with a different set of
    # findings, or one after a run that stopped partway, posts only what is not there yet.
    found = set().union(*(carried(head, c.get("body")) for c in review_comments(client, headers, slug, number)
                          if (c.get("user") or {}).get("id") == me))
    rows = [row for row in rows if marker(head, row) not in found]
    if not rows:
        return 0, 0, True
    unmoved(project, number, head)
    endpoint = f"https://api.github.com/repos/{slug}/pulls/{number}/reviews"
    mark = marker(head, rows)

    def submit(part):
        client.post(endpoint, headers=headers, json={
            "commit_id": head, "event": "COMMENT", "body": mark,
            "comments": [dict(path=path, line=line, side="RIGHT", body=f"{body}\n\n{marker(head, [path, line, body])}")
                         for path, line, body in part]}).raise_for_status()

    try:
        submit(rows)
        return len(rows), 0, False
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 422:
            raise
    # GitHub takes or refuses a review whole, so one finding per review keeps a refused line to itself.
    posted = refused = 0
    for row in rows:
        try:
            submit([row])
            posted += 1
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 422:
                raise
            refused += 1
    return posted, refused, False


def gitlab(client, headers, project, base, slug, number, rows, me, head):
    """(posted, refused, already present) as one discussion per finding on the reviewed head."""
    root = f"{base}/projects/{quote(slug, safe='')}/merge_requests/{number}"
    response = client.get(root, headers=headers)
    response.raise_for_status()
    current = response.json()
    refs = (current.get("diff_refs") or {}) if isinstance(current, dict) else {}
    if refs.get("head_sha") != head or not all(isinstance(refs.get(k), str) and SHA.fullmatch(refs[k]) for k in ("base_sha", "start_sha")):
        raise Stale("the change moved on since the review")
    found, page = set(), 1
    while True:
        response = client.get(root + "/discussions", params={"page": page, "per_page": 100}, headers=headers)
        response.raise_for_status()
        # An answer that is not a list could hide an earlier post of ours behind a duplicate.
        if not isinstance(response.json(), list):
            raise ReviewError("Invalid merge request discussion list")
        # Only the first note of a diff discussion carries a marker of ours.
        found |= set().union(*(carried(head, d["notes"][0].get("body")) for d in response.json() if isinstance(d, dict) and d.get("notes")
                              and d["notes"][0].get("type") == "DiffNote" and (d["notes"][0].get("author") or {}).get("id") == me))
        if not response.headers.get("X-Next-Page"):
            break
        # An unread page could hold an earlier post of ours, so stop rather than risk a duplicate.
        if page >= PAGES:
            raise ReviewError("Too many discussions to check for earlier inline comments")
        page += 1
    pending = [row for row in rows if marker(head, row) not in found]
    if not pending:
        return 0, 0, True
    unmoved(project, number, head)
    posted = refused = 0
    for path, line, body in pending:
        try:
            client.post(root + "/discussions", headers=headers, json={
                "body": f"{body}\n\n{marker(head, [path, line, body])}", "commit_id": head,
                "position": {"position_type": "text", "base_sha": refs["base_sha"], "head_sha": head, "start_sha": refs["start_sha"],
                             "new_path": path, "old_path": path, "new_line": line}}).raise_for_status()
        except httpx.HTTPStatusError as error:
            # Only the forge's own rejection of a position is that finding's problem alone.
            if error.response.status_code not in (400, 422):
                raise
            refused += 1
        else:
            posted += 1
    return posted, refused, False


def post(address, value, project, checkout):
    """(outcome, posted, refused, left out). Raises on any read or transport failure."""
    rows, left = comments(value)
    head = value["head"]
    if not rows:
        return "skipped", 0, 0, left
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ReviewError("The reviewed head is not a commit SHA")
    number = address.rsplit("#", 1)[1]
    with forge_status.connection(address, value["source"], checkout) as (client, provider, slug, base, headers):
        if provider == "github":
            sent = github(client, headers, project, slug, number, rows, reviewer(project, number, value), head)
        else:
            sent = gitlab(client, headers, project, base, slug, number, rows, account(client, headers, base), head)
    posted, refused, present = sent
    return "already-present" if present else "posted" if posted else "failed", posted, refused, left


def outcome(args, value):
    """Keys for the report. Inline comments never change the review's own result or exit code."""
    if not (args.change and args.post and not args.estimate and value.get("publication") == "posted-or-already-present"
            and (getattr(args, "inline_comments", False) or os.getenv("REVIEW_INLINE_COMMENTS") == "1")):
        return {"inline_publication": "skipped"}
    try:
        result, posted, refused, left = post(args.change, value, getattr(args, "forge_project", None), args.repo)
    except Stale as error:
        log.warning("Inline comments for %s were not posted (%s)", args.change, error)
        return {"inline_publication": "stale"}
    # This path bypasses the helper's publication checks, so it must not be able to fail the review itself.
    except Exception as error:
        reason = str(error) if isinstance(error, ReviewError) else (
            f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else type(error).__name__)
        log.warning("Inline comments for %s were not posted (%s)", args.change, reason)
        return {"inline_publication": "failed"}
    if refused or left:
        log.warning("Inline comments for %s: %d posted, %d refused by the forge or left out", args.change, posted, refused + left)
    return {"inline_publication": result, "inline_posted": posted, "inline_refused": refused + left}
