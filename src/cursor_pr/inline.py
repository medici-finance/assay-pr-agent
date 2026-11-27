"""Post optional inline comments on the reviewed head after a successful summary."""
from dataclasses import dataclass
import fcntl
import hashlib
import json
import logging
import os
import re
from urllib.parse import quote

import httpx

from . import forge_status, providers
from .scanners import fence, safe_path
from .setup import state_dir
from .source import ReviewError, check_publication, run_forge

log = logging.getLogger(__name__)
PREFIX = "<!-- assay-pr-agent:inline:%s:"
MARK = PREFIX + "%s -->"
# Caps for the two code spans; the summary comment carries each finding in full.
CAPS = (255, 2000)
PAGES = 20
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class Stale(ReviewError):
    """The change moved on since the review, so its lines may no longer be the same code."""


@dataclass
class Progress:
    posted: int = 0
    refused: int = 0


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


def github_comments(client, headers, endpoint):
    """Read complete pages without trusting a server-supplied URL for the next request."""
    for page in range(1, PAGES + 1):
        response = client.get(endpoint, params={"page": page, "per_page": 100}, headers=headers)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ReviewError("Invalid GitHub comment list")
        yield from (row for row in rows if isinstance(row, dict))
        if len(rows) < 100 and "next" not in response.links:
            return
    raise ReviewError("Too many GitHub comments to check for earlier review publication")


def reviewer(client, headers, slug, number, value):
    # GitHub identifies the App that actually posted a summary, independently of its comment text.
    # The same configured App ID is used when the forge helper mints the installation token.
    app_id = os.getenv("REVIEW_FORGE_APP_ID", "")
    if not re.fullmatch(r"[1-9][0-9]*", app_id):
        raise ReviewError("Could not identify the reviewer identity on the change")
    first = f"{providers.LABELS[value.get('provider', 'cursor')]} review of {value['head']}"
    endpoint = f"https://api.github.com/repos/{slug}/issues/{number}/comments"
    for row in github_comments(client, headers, endpoint):
        integration, author = row.get("performed_via_github_app") or {}, row.get("user") or {}
        if (type(integration.get("id")) is int and integration["id"] == int(app_id) and author.get("type") == "Bot"
                and type(author.get("id")) is int and author["id"] > 0
                and str(row.get("body") or "").split("\n", 1)[0] == first):
            return author["id"]
    raise ReviewError("Could not identify the reviewer identity on the change")


def account(client, headers, base):
    """The reviewer's own user on GitLab: a project access token acts as its bot user."""
    response = client.get(base + "/user", headers=headers)
    response.raise_for_status()
    user = response.json()
    me = user.get("id") if isinstance(user, dict) else None
    if type(me) is not int or me < 1:
        raise ReviewError("Could not identify the reviewer identity on the change")
    return me


def unmoved(project, number, head):
    if run_forge("change", project, numbers=[number]).get("headSha") != head:
        raise Stale("the change moved on since the review")


def authorize(project, number, head, rows, previous):
    unmoved(project, number, head)
    checked = []
    for path, line, body in rows:
        old = previous.get(path) or path
        if not safe_path(old):
            raise ReviewError("Invalid previous path for an inline comment")
        marked = f"{body}\n\n{marker(head, [path, line, body])}"
        checked.extend((fence(path), fence(old), marked))
    check_publication(project, number, head, "\n\n".join(checked))


def github(client, headers, project, slug, number, rows, me, head, progress):
    """Return whether all findings were already present, retaining each posting outcome."""
    # Each comment closes with its own marker, as on GitLab, so a later run with a different set of
    # findings, or one after a run that stopped partway, posts only what is not there yet.
    root = f"https://api.github.com/repos/{slug}/pulls/{number}"
    found = set().union(*(carried(head, c.get("body")) for c in github_comments(client, headers, root + "/comments")
                          if (c.get("user") or {}).get("id") == me))
    rows = [row for row in rows if marker(head, row) not in found]
    if not rows:
        return True
    authorize(project, number, head, rows, {})
    endpoint = root + "/reviews"

    def submit(part):
        client.post(endpoint, headers=headers, json={
            "commit_id": head, "event": "COMMENT", "body": marker(head, part),
            "comments": [dict(path=path, line=line, side="RIGHT", body=f"{body}\n\n{marker(head, [path, line, body])}")
                         for path, line, body in part]}).raise_for_status()

    try:
        submit(rows)
        progress.posted += len(rows)
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 422:
            raise
        if len(rows) == 1:
            progress.refused += 1
        else:
            # The forge rejects a batch in full, so a singleton records only what it actually posted.
            for row in rows:
                # Time has passed since the batch was authorized, and a push in it would put the
                # rest of these on a commit that is no longer the head.
                unmoved(project, number, head)
                try:
                    submit([row])
                    progress.posted += 1
                except httpx.HTTPStatusError as refusal:
                    if refusal.response.status_code != 422:
                        raise
                    progress.refused += 1
    return False


def gitlab(client, headers, project, base, slug, number, rows, me, head, previous, progress):
    """Post pending findings as discussions, retaining each posting outcome."""
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
        # Either way GitLab has of saying there is more. The next request is still built here.
        if not response.headers.get("X-Next-Page") and "next" not in response.links:
            break
        # An unread page could hold an earlier post of ours, so stop rather than risk a duplicate.
        if page >= PAGES:
            raise ReviewError("Too many discussions to check for earlier inline comments")
        page += 1
    pending = [row for row in rows if marker(head, row) not in found]
    if not pending:
        return True
    authorize(project, number, head, pending, previous)
    for path, line, body in pending:
        try:
            client.post(root + "/discussions", headers=headers, json={
                "body": f"{body}\n\n{marker(head, [path, line, body])}", "commit_id": head,
                "position": {"position_type": "text", "base_sha": refs["base_sha"], "head_sha": head, "start_sha": refs["start_sha"],
                             "new_path": path, "old_path": previous.get(path) or path, "new_line": line}}).raise_for_status()
        except httpx.HTTPStatusError as error:
            # Only the forge's own rejection of a position is that finding's problem alone.
            if error.response.status_code not in (400, 422):
                raise
            progress.refused += 1
        else:
            progress.posted += 1
    return False


def post(address, value, project, state, rows, previous, progress):
    head, url = value["head"], value["source"]
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ReviewError("The reviewed head is not a commit SHA")
    number = int(address.rsplit("#", 1)[1])
    provider, slug, _ = forge_status.change(address, url)
    if not project or project.provider != provider or project.slug != slug or project.change_url(number) != url:
        raise ReviewError("The inline comment target is not a configured review project")
    directory = state / "inline-publications"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(json.dumps([project.key, number, head]).encode()).hexdigest()
    with (directory / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with forge_status.connection(address, url, project.checkout) as (client, _, _, base, headers):
            if provider == "github":
                me = reviewer(client, headers, slug, number, value)
                present = github(client, headers, project, slug, number, rows, me, head, progress)
            else:
                me = account(client, headers, base)
                present = gitlab(client, headers, project, base, slug, number, rows, me, head, previous, progress)
    return "already-present" if present else "posted" if progress.posted else "failed"


def outcome(args, value):
    """Keys for the report. Inline comments never change the review's own result or exit code."""
    if not (args.change and args.post and not args.estimate and value.get("publication") == "posted-or-already-present"
            and (getattr(args, "inline_comments", False) or os.getenv("REVIEW_INLINE_COMMENTS") == "1")):
        return {"inline_publication": "skipped"}
    progress = Progress()
    try:
        rows, progress.refused = comments(value)
        result = "skipped"
        if rows:
            state = getattr(args, "forge_state", None) or state_dir()
            previous = getattr(args, "inline_previous_paths", {})
            result = post(args.change, value, getattr(args, "forge_project", None), state, rows, previous, progress)
    except Stale as error:
        log.warning("Inline comments for %s were not posted (%s)", args.change, error)
        result = "stale"
    except Exception as error:
        reason = str(error) if isinstance(error, ReviewError) else (
            f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else type(error).__name__)
        log.warning("Inline comment publication for %s stopped (%s)", args.change, reason)
        result = "failed"
    if progress.refused:
        log.warning("Inline comments for %s: %d posted, %d refused by the forge or left out",
                    args.change, progress.posted, progress.refused)
    return {"inline_publication": result, "inline_posted": progress.posted, "inline_refused": progress.refused}
