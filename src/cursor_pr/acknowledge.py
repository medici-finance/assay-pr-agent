"""Mark a queued review request on the forge, and tell the requester when nothing was posted."""
import logging
import os
import subprocess
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
NOTICE = ("The `/review` request {request} was not completed: the review could not be finished or "
          "published. Post `/review` again to retry it.\n")
NOTE = {"github": "#issuecomment-", "gitlab": "#note_"}
# A removal or notice that failed is tried again after this long, then after twice as long each time
# up to an hour, and given up after this many tries, about six hours in all.
RETRY_SECONDS = 60
RETRY_CAP_SECONDS = 3600
TRIES = 12
# A forge that answers slowly must not hold the queue back, so every forge call made here runs
# under a deadline: none starts after it and none waits past it. What is left then is done later.
# One cycle's pass over the markers to write, and its pass over the removals and notices due:
BUDGET_SECONDS = 20
# The marker of one request, written just before its review if the pass did not get to it:
START_SECONDS = 5
# The removal and the notice of one request, right after its review:
FINISH_SECONDS = 20
# What the notice may still take to start when the removal before it used that time up. The two
# share the count of tries, so a removal that is slow every time must not keep the notice from
# ever being tried.
NOTICE_SECONDS = 5
TIMEOUT_SECONDS = 15
HELPER_SECONDS = 45
PAGES = 5


class OutOfTime(ReviewError):
    """A deadline passed before the call was made. Nothing was tried, so nothing failed."""

    def __init__(self):
        super().__init__("no time left for it in this cycle")


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
        raise OutOfTime()
    with httpx.Client(timeout=left, follow_redirects=False,
                      **(client_options() if project.provider == "gitlab" else {})) as client:
        try:
            return client.request(method, url, headers=headers, **options)
        except httpx.TimeoutException:
            if left < TIMEOUT_SECONDS:
                # Cut short by the deadline, not by the forge: it had less than its usual time.
                raise OutOfTime() from None
            raise


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


def add(project, number, comment, deadline=None):
    """Mark the request. Marking twice is the same as marking once, on either forge."""
    root, markers, headers = reviewer(project, number, comment)
    if project.provider == "gitlab":
        if own_award(project, root, markers, headers, deadline):
            return  # An account can award an emoji once, so this one already says a request is waiting.
        request(project, "POST", markers, headers, deadline, params={"name": MARK["gitlab"][0]}).raise_for_status()
    else:
        reaction_id(react(project, markers, headers, deadline))


def remove(project, number, comment, deadline=None, said=False):
    """Remove the reviewer's marker, wherever it came from. Nothing about it is kept here: the forge is asked.

    GitLab lists the awards with their owners. GitHub tells an account the ID of its reaction only
    in the answer to creating it, which returns the existing one, so the reaction is asked for and
    then deleted. An answer that is final ends it: there is nothing of ours left, or nothing this
    credential may remove any longer, which gets a log line unless `said`: the same refusal was
    then logged already, when the request was to be marked.
    """
    root, markers, headers = reviewer(project, number, comment)
    try:
        if project.provider == "gitlab":
            ident = own_award(project, root, markers, headers, deadline)
        else:
            response = react(project, markers, headers, deadline)
            if refused(response):
                # Refused when the request was to be marked too, this credential has no reaction
                # there. Refused only now, one written earlier may stay.
                return None if said else left_in_place(project, number, comment, response)
            ident = reaction_id(response)
    except httpx.HTTPStatusError as err:
        if not refused(err.response):
            raise
        return left_in_place(project, number, comment, err.response)
    if ident is not None:
        response = request(project, "DELETE", f"{markers}/{ident}", headers, deadline)
        if not refused(response):
            response.raise_for_status()
        else:
            left_in_place(project, number, comment, response)


def left_in_place(project, number, comment, response):
    # Gone with its comment or merge request: nothing to say. Anything else final may leave a marker behind.
    if response.status_code not in (404, 410):
        log.warning("The queued marker on %s#%d comment %d may be left in place: HTTP %d",
                    project.slug, number, comment, response.status_code)


def notify(project, number, comment, state, deadline=None):
    # The helper publishes only on the change's current head. The notice is about the request, not a
    # head, so it goes on whatever head the change has now, once per head like any comment.
    left = HELPER_SECONDS if deadline is None else min(HELPER_SECONDS, deadline - time.monotonic())
    if left <= 0:
        raise OutOfTime()
    try:
        head = run_forge("change", project, numbers=[number], timeout=left).get("headSha")
    except subprocess.TimeoutExpired:
        if left < HELPER_SECONDS:
            raise OutOfTime() from None  # Stopped by the deadline before its usual time was up.
        raise
    if not valid_sha(head):
        raise ReviewError("Forge did not identify the change head")
    if deadline is not None and deadline <= time.monotonic():
        raise OutOfTime()
    # Started within the deadline and then never cut short: a helper stopped just after the forge
    # took the comment would leave no receipt, and the next try would post the notice again.
    request = f"{project.change_url(number)}{NOTE[project.provider]}{comment}"
    post_comment(project, number, head, NOTICE.format(request=request), state)


class Acknowledgement:
    """Every call fails open: one log line, and the queue and the review go on.

    Nothing is kept about a marker. Whether a request is marked, and under which ID, is the forge's
    to say, and writing a marker twice or removing one that is not there does no harm. A request's
    record holds only what it still owes once its review has finished: the marker's removal and,
    after a failure, the notice.
    """

    def __init__(self, jobs, state):
        self.jobs = jobs
        self.state = state
        # Requests whose marker this process has settled: written, or refused for good.
        self.marked = set()
        # Those of them it was refused for, which has been logged.
        self.refused = set()

    def queued(self, project, number, comment, deadline=None):
        job = project.key, number, comment

        def mark():
            try:
                add(project, number, comment, deadline)
            except httpx.HTTPStatusError as err:
                if refused(err.response):
                    self.marked.add(job)  # Not to be asked for again. An answer for now only is.
                    self.refused.add(job)
                raise
            self.marked.add(job)
        self.attempt(mark, "mark", project, number, comment)

    def finished(self, project, number, comment, status, deadline=None):
        job = project.key, number, comment
        self.marked.discard(job)
        said = job in self.refused
        self.refused.discard(job)
        owed = self.jobs.acknowledgement(job)
        if owed is None:
            return
        notice_sent, marker_cleared = owed
        if deadline is None:
            deadline = time.monotonic() + FINISH_SECONDS
        in_time = time.monotonic() < deadline

        def clear():
            if project.provider == "gitlab" and self.jobs.pending(project.key, number):
                # GitLab's marker stays while another request is waiting; its last request clears it.
                # Each of them gets a record for that, also one queued by a version that kept none.
                self.jobs.acknowledge_pending(project.key, number)
            else:
                remove(project, number, comment, deadline, said)
            # Kept apart from the notice: while only the notice is owed, the marker is left alone.
            self.jobs.record_cleared(job)

        cleared = bool(marker_cleared) or self.attempt(clear, "clear the queued marker on", project, number, comment)

        def report_failure():
            notify(project, number, comment, self.state,
                   max(deadline, time.monotonic() + NOTICE_SECONDS) if in_time else deadline)
            self.jobs.record_notice(job)

        notice_needed = status == "failed" and not notice_sent
        notified = not notice_needed or self.attempt(
            report_failure, "report the failed review on", project, number, None)
        if notified and cleared:
            self.jobs.clear_acknowledgement(job)
            return
        if False not in (notified, cleared):
            return  # Only out of time: nothing was tried, so no try is counted and it stays due.
        tries = self.jobs.acknowledgement_tries(job) + 1
        if tries >= TRIES:
            log.warning("Gave up on the queued marker or failure notice of %s#%d comment %d after %d tries",
                        project.slug, number, comment, tries)
            self.jobs.clear_acknowledgement(job)
        else:
            self.jobs.retry_acknowledgement(job, time.time() + min(RETRY_SECONDS * 2 ** (tries - 1), RETRY_CAP_SECONDS))

    def settle(self, projects):
        """Try again what a finished request still owes the forge and is due: its marker's removal, its notice."""
        deadline = time.monotonic() + BUDGET_SECONDS
        for key, number, comment, status in self.jobs.unfinished_acknowledgements(time.time()):
            if key in projects and time.monotonic() < deadline:
                self.finished(projects[key], number, comment, status, deadline)

    def mark(self, projects):
        """Mark the waiting requests: the new ones, and those a poller that stopped left unmarked.

        All of it ends within the budget. A request left unmarked is marked when its review starts.
        """
        deadline = time.monotonic() + BUDGET_SECONDS
        for key, number, comment in self.jobs.waiting_acknowledgements():
            if key in projects and (key, number, comment) not in self.marked and time.monotonic() < deadline:
                self.queued(projects[key], number, comment, deadline)

    def starting(self, project, number, comment):
        """Mark a request whose review is about to start, if nothing has marked it yet. It is given a short time."""
        job = project.key, number, comment
        if job not in self.marked and self.jobs.acknowledgement(job) is not None:
            self.queued(project, number, comment, time.monotonic() + START_SECONDS)

    @staticmethod
    def attempt(action, verb, project, number, comment):
        """True when it was done, False when it failed, None when there was no time left to try."""
        try:
            action()
            return True
        except OutOfTime:
            return None  # Not a failure, and not worth a line: it is done in a later cycle.
        except Exception as err:
            # httpx errors quote the request URL. Report the status, or the class, and nothing else.
            detail = f"HTTP {err.response.status_code}" if isinstance(err, httpx.HTTPStatusError) else (
                     str(err) if isinstance(err, ReviewError) else type(err).__name__)
            log.warning("Could not %s %s#%d%s: %s", verb, project.slug, number,
                        f" comment {comment}" if comment else "", detail)
            return False
