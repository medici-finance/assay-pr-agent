"""Mark a queued review request on the forge, and tell the requester when nothing was posted."""
import logging
import os
from urllib.parse import quote

import httpx

from .gitlab_tls import client_options
from .source import ReviewError, post_comment, reviewer_token, run_forge, valid_sha

log = logging.getLogger(__name__)
# The forge helper has no reaction operation, so the marker goes straight to each forge's API.
MARK = {"github": ("eyes", "content"), "gitlab": ("hourglass", "name")}
NOTICE = ("A `/review` request on this change did not produce a posted review: the review could not be "
          "completed or published. Post `/review` again to retry it.\n")
PAGES = 5


def request(project, method, url, headers, **options):
    # The posture of the GitLab note lookup: no redirects, and a private CA only for its own instance.
    with httpx.Client(timeout=15, follow_redirects=False,
                      **(client_options() if project.provider == "gitlab" else {})) as client:
        return client.request(method, url, headers=headers, **options)


def reviewer(project, number, comment):
    """The API root, the URL listing this request's markers, and headers that hold the token."""
    if type(number) is not int or number < 1 or type(comment) is not int or comment < 1:
        raise ReviewError("Invalid review request address")
    base = os.environ.get("GITLAB_API_BASE", "").rstrip("/")
    if project.provider == "github":
        root = "https://api.github.com"
        markers = root + "/repos/" + quote(project.slug, safe="/") + f"/issues/comments/{comment}/reactions"
    elif base == project.url.removesuffix("/" + project.slug) + "/api/v4":
        # The award sits on the merge request, so it covers every request waiting there.
        root = base
        markers = f"{base}/projects/{quote(project.slug, safe='')}/merge_requests/{number}/award_emoji"
    else:
        raise ReviewError("GITLAB_API_BASE must match the project's HTTPS instance /api/v4")
    token = reviewer_token(project.provider, project.slug, project.checkout)
    if project.provider == "github":
        return root, markers, {"Authorization": "Bearer " + token}
    return root, markers, {"PRIVATE-TOKEN": token}


def own_award(project, root, markers, headers):
    """ID of this credential's GitLab award, if present."""
    response = request(project, "GET", root + "/user", headers)
    response.raise_for_status()
    account = response.json()
    if not isinstance(account, dict) or type(account.get("id")) is not int or account["id"] < 1:
        raise ReviewError("The forge did not identify the reviewer account")
    for page in range(1, PAGES + 1):
        response = request(project, "GET", markers, headers, params={"per_page": 100, "page": page})
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ReviewError("Invalid marker listing")
        for row in rows:
            if (isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0
                    and isinstance(row.get("user"), dict) and type(row["user"].get("id")) is int
                    and row["user"]["id"] == account["id"] and row.get("name") == "hourglass"):
                return row["id"]
        if len(rows) < 100:
            return None
    raise ReviewError("Too many markers to search")


def add(project, number, comment):
    root, markers, headers = reviewer(project, number, comment)
    emoji, field = MARK[project.provider]
    if field == "name":
        if own_award(project, root, markers, headers):
            return  # An account can award an emoji once, so this one already says a request is waiting.
        response = request(project, "POST", markers, headers, params={"name": emoji})
    else:
        response = request(project, "POST", markers, headers, json={"content": emoji})
    response.raise_for_status()  # GitHub answers 200 when this account has already reacted.
    if project.provider == "github":
        row = response.json()
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1 or row.get(field) != emoji:
            raise ReviewError("The forge did not identify the reviewer's reaction")
        return row["id"]


def remove(project, number, comment, marker_id):
    if project.provider == "github" and marker_id is None:
        return
    root, markers, headers = reviewer(project, number, comment)
    ident = marker_id if project.provider == "github" else own_award(project, root, markers, headers)
    if ident is not None:
        response = request(project, "DELETE", f"{markers}/{ident}", headers)
        if response.status_code != 404:
            response.raise_for_status()


def notify(project, number, state):
    # The helper publishes only on the change's current head. The notice is about the request, not a
    # head, so it goes on whatever head the change has now, once per head like any comment.
    head = run_forge("change", project, numbers=[number]).get("headSha")
    if not valid_sha(head):
        raise ReviewError("Forge did not identify the change head")
    post_comment(project, number, head, NOTICE, state)


class Acknowledgement:
    """Every call fails open: one log line, and the queue and the review go on."""

    def __init__(self, jobs, state):
        self.jobs = jobs
        self.state = state

    def queued(self, project, number, comment):
        def mark():
            marker = add(project, number, comment)
            if marker is not None:
                self.jobs.record_marker((project.key, number, comment), marker)
        self.attempt(mark, "mark", project, number, comment)

    def finished(self, project, number, comment, status):
        job = project.key, number, comment
        acknowledgement = self.jobs.acknowledgement(job)
        if acknowledgement is None:
            return
        marker, notice_sent = acknowledgement

        def report_failure():
            notify(project, number, self.state)
            self.jobs.record_notice(job)

        notice_needed = status == "failed" and not notice_sent
        notified = not notice_needed or self.attempt(
            report_failure, "report the failed review on", project, number, None)

        def clear():
            # GitLab's marker stays while another request is waiting; its last request clears it.
            if project.provider != "gitlab" or not self.jobs.pending(project.key, number):
                remove(project, number, comment, marker)

        cleared = self.attempt(clear, "clear the queued marker on", project, number, comment)
        if notified and cleared:
            self.jobs.clear_acknowledgement(job)

    @staticmethod
    def attempt(action, verb, project, number, comment):
        try:
            action()
            return True
        except Exception as err:
            # httpx errors quote the request URL. Report the status, or the class, and nothing else.
            detail = f"HTTP {err.response.status_code}" if isinstance(err, httpx.HTTPStatusError) else (
                     str(err) if isinstance(err, ReviewError) else type(err).__name__)
            log.warning("Could not %s %s#%d%s: %s", verb, project.slug, number,
                        f" comment {comment}" if comment else "", detail)
            return False
