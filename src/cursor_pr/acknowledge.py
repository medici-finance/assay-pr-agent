"""Mark a queued review request on the forge, and tell the requester when nothing was posted."""
import logging
import os
import time
from urllib.parse import quote

import httpx

from .gitlab_tls import client_options
from .source import ReviewError, post_comment, reviewer_token, run_forge, valid_sha

log = logging.getLogger(__name__)
# The forge helper has no reaction operation, so the marker goes straight to each forge's API.
MARK = {"github": ("eyes", "content"), "gitlab": ("hourglass", "name")}
# Names its request, so that a second request failing at the same head gets a notice of its own:
# the helper posts a given text once per head.
NOTICE = ("The `/review` request {request} did not produce a posted review: the review could not be "
          "completed or published. Post `/review` again to retry it.\n")
NOTE = {"github": "#issuecomment-", "gitlab": "#note_"}
# A removal or notice that failed is tried again after this long, then after twice as long each time
# up to an hour, and given up after this many tries, about six hours in all.
RETRY_SECONDS = 60
RETRY_CAP_SECONDS = 3600
TRIES = 12
# How long one cycle spends on markers and notices before it goes on to the reviews. What is left
# waits for the next cycle: a forge that answers slowly must not hold the queue back.
BUDGET_SECONDS = 20
# How long the marker of one request may take when it is written just before that request's review.
START_SECONDS = 5
TIMEOUT_SECONDS = 15
PAGES = 5
# Stored in place of a GitHub reaction's ID from just before the reaction is asked for until the
# answer is recorded. A poller that stopped in between, or an answer that never arrived, leaves it:
# a reaction may then exist whose ID nobody kept.
UNKNOWN = 0
# Stored from the moment a request is queued until its marker has been asked for once. A poller that
# stopped in between finds it at its next start and marks the request then.
PENDING = -1


def refused(response):
    """Whether the forge turned the request down for good, and not only for now."""
    limited = (response.status_code in (401, 408, 429) or "retry-after" in response.headers
               or response.headers.get("x-ratelimit-remaining") == "0")
    # A redirect counts: none is followed, and a project that moved is not this credential's to write to.
    return 300 <= response.status_code < 500 and not limited


def request(project, method, url, headers, deadline=None, **options):
    # The posture of the GitLab note lookup: no redirects, and a private CA only for its own instance.
    # With a deadline, no request starts after it and none waits past it for the forge.
    left = TIMEOUT_SECONDS if deadline is None else min(TIMEOUT_SECONDS, deadline - time.monotonic())
    if left <= 0:
        raise ReviewError("no time left for it in this cycle")
    with httpx.Client(timeout=left, follow_redirects=False,
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


def own_award(project, root, markers, headers, deadline=None):
    """ID of this credential's GitLab award, if present."""
    response = request(project, "GET", root + "/user", headers, deadline)
    response.raise_for_status()
    account = response.json()
    if not isinstance(account, dict) or type(account.get("id")) is not int or account["id"] < 1:
        raise ReviewError("The forge did not identify the reviewer account")
    for page in range(1, PAGES + 1):
        response = request(project, "GET", markers, headers, deadline, params={"per_page": 100, "page": page})
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


def react(project, markers, headers, deadline=None):
    """Ask GitHub for the reviewer's reaction and return the answer. It creates one only if there is none."""
    return request(project, "POST", markers, headers, deadline, json={"content": MARK["github"][0]})


def reaction_id(response):
    response.raise_for_status()  # GitHub answers 200 when this account has already reacted.
    row = response.json()
    if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1 or row.get("content") != MARK["github"][0]:
        raise ReviewError("The forge did not identify the reviewer's reaction")
    return row["id"]


def add(project, number, comment, sending=lambda: None, deadline=None):
    """Mark the request. sending is called just before GitHub is asked to create the reaction."""
    root, markers, headers = reviewer(project, number, comment)
    if project.provider == "gitlab":
        if own_award(project, root, markers, headers, deadline):
            return  # An account can award an emoji once, so this one already says a request is waiting.
        request(project, "POST", markers, headers, deadline, params={"name": MARK["gitlab"][0]}).raise_for_status()
        return
    if deadline is not None and deadline <= time.monotonic():
        raise ReviewError("no time left for it in this cycle")  # Before the note that a reaction may exist.
    sending()
    return reaction_id(react(project, markers, headers, deadline))


def recover(project, number, comment):
    """The ID of a GitHub reaction that may exist without its ID having been kept, or None if there is none."""
    # Asking again is how GitHub tells an account the ID of the reaction it already has. Refused for
    # good, as it is without the permission or once the comment is gone, there is none of ours to
    # remove. Turned away for now, as by a rate limit, the removal stays owed.
    root, markers, headers = reviewer(project, number, comment)
    response = react(project, markers, headers)
    return None if refused(response) else reaction_id(response)


def remove(project, number, comment, marker_id):
    if project.provider == "github" and marker_id in (None, PENDING):
        return
    root, markers, headers = reviewer(project, number, comment)
    try:
        ident = marker_id if project.provider == "github" else own_award(project, root, markers, headers)
    except httpx.HTTPStatusError as err:
        if refused(err.response):
            return  # The merge request is gone or closed to this credential, and the award with it.
        raise
    if ident is not None:
        response = request(project, "DELETE", f"{markers}/{ident}", headers)
        if not refused(response):  # Gone already, or no longer this credential's to remove.
            response.raise_for_status()


def notify(project, number, comment, state):
    # The helper publishes only on the change's current head. The notice is about the request, not a
    # head, so it goes on whatever head the change has now, once per head like any comment.
    head = run_forge("change", project, numbers=[number]).get("headSha")
    if not valid_sha(head):
        raise ReviewError("Forge did not identify the change head")
    request = f"{project.change_url(number)}{NOTE[project.provider]}{comment}"
    post_comment(project, number, head, NOTICE.format(request=request), state)


class Acknowledgement:
    """Every call fails open: one log line, and the queue and the review go on."""

    def __init__(self, jobs, state):
        self.jobs = jobs
        self.state = state

    def queued(self, project, number, comment, deadline=None):
        job = project.key, number, comment

        def mark():
            try:
                marker = add(project, number, comment, lambda: self.jobs.record_marker(job, UNKNOWN), deadline)
            except httpx.HTTPStatusError as err:
                if err.response.status_code < 500:
                    self.jobs.record_marker(job, None)  # Answered and turned down: nothing was created.
                raise
            self.jobs.record_marker(job, marker)  # Asked for. GitLab's award has no ID to keep.
        self.attempt(mark, "mark", project, number, comment)

    def finished(self, project, number, comment, status):
        job = project.key, number, comment
        acknowledgement = self.jobs.acknowledgement(job)
        if acknowledgement is None:
            return
        marker, notice_sent, marker_cleared = acknowledgement

        def report_failure():
            notify(project, number, comment, self.state)
            self.jobs.record_notice(job)

        notice_needed = status == "failed" and not notice_sent
        notified = not notice_needed or self.attempt(
            report_failure, "report the failed review on", project, number, None)

        def clear():
            if project.provider == "gitlab" and self.jobs.pending(project.key, number):
                # GitLab's marker stays while another request is waiting; its last request clears it.
                # Each of them gets a record for that, also one queued by a version that kept none.
                self.jobs.acknowledge_pending(project.key, number)
            else:
                found = marker
                if project.provider == "github" and marker == UNKNOWN:
                    # Kept before it is removed: if the removal fails, the next try must not ask
                    # for the reaction again, which would put one on a request that has finished.
                    found = recover(project, number, comment)
                    self.jobs.record_marker(job, found)
                remove(project, number, comment, found)
            # Kept apart from the notice: while only the notice is owed, the marker is left alone.
            self.jobs.record_cleared(job)

        cleared = bool(marker_cleared) or self.attempt(clear, "clear the queued marker on", project, number, comment)
        if notified and cleared:
            self.jobs.clear_acknowledgement(job)
            return
        tries = self.jobs.acknowledgement_tries(job) + 1
        if tries >= TRIES:
            log.warning("Gave up on the queued marker or failure notice of %s#%d comment %d after %d tries",
                        project.slug, number, comment, tries)
            self.jobs.clear_acknowledgement(job)
        else:
            self.jobs.retry_acknowledgement(job, time.time() + min(RETRY_SECONDS * 2 ** (tries - 1), RETRY_CAP_SECONDS))

    def settle(self, projects):
        """Try again what a finished request still owes the forge and is due: its marker's removal, its notice."""
        started = time.monotonic()
        for key, number, comment, status in self.jobs.unfinished_acknowledgements(time.time()):
            if key in projects and time.monotonic() - started < BUDGET_SECONDS:
                self.finished(projects[key], number, comment, status)

    def mark(self, projects):
        """Mark the waiting requests that have not been marked: the new ones, and those a stopped poller left.

        All of it ends within the budget: no forge call waits past it. A request left unmarked is
        marked when its review starts.
        """
        deadline = time.monotonic() + BUDGET_SECONDS
        for key, number, comment in self.jobs.unmarked():
            if key in projects and time.monotonic() < deadline:
                self.queued(projects[key], number, comment, deadline)

    def starting(self, project, number, comment):
        """Mark a request whose review is about to start, if nothing has marked it yet. It is given a short time."""
        acknowledgement = self.jobs.acknowledgement((project.key, number, comment))
        if acknowledgement is not None and acknowledgement[0] == PENDING:
            self.queued(project, number, comment, time.monotonic() + START_SECONDS)

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
