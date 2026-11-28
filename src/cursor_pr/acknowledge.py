"""Mark a queued review request on the forge, and tell the requester when nothing was posted."""
import logging
import os
import queue
import threading
import time
from urllib.parse import quote, urlsplit

import httpx

from .discovery import COMMANDS
from .gitlab_tls import client_options
from .source import ReviewError, post_comment, reviewer_token, run_forge, valid_sha

log = logging.getLogger(__name__)
# The forge helper has no reaction operation, so the marker goes straight to each forge's API.
MARK = {"github": ("eyes", "content"), "gitlab": ("hourglass", "name")}
# Names its request, so that a second request failing at the same head gets a notice of its own:
# the helper posts a given text once per head.
NOTICE = ("The `{command}` request {request} was not completed: the review could not be finished or "
          "published. Post `{command}` again to retry it.\n")
NOTE = {"github": "#issuecomment-", "gitlab": "#note_"}
# A removal or a notice that failed is tried again after this long, then after twice as long each
# time up to an hour, and given up after this many tries, about six hours in all. Each of the two
# has its own count and its own time: neither can use up the other's tries. A try is one that
# reached the forge or the helper, however it ended; a call never started for lack of time is none.
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
TIMEOUT_SECONDS = 15
SHORTEST_SECONDS = 1
HELPER_SECONDS = 45
PAGES = 5


# How many calls to a forge or to the helper have been started. One action makes several, and
# running out of time between two of them is a try that was made, not one that was not.
started = [0]


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


# For each forge host, the one request it has not answered within its time, still running in its thread.
unanswered = {}


class Busy(OutOfTime):
    """The forge still has a request of ours open. Nothing was sent, so nothing was tried."""

    def __init__(self):
        ReviewError.__init__(self, "an earlier request to the forge is still unanswered")


def request(project, method, url, headers, deadline=None, **options):
    """One request to the forge API, over within its time whatever the forge does.

    The posture of the GitLab note lookup: no redirects, and a private CA only for its own instance.
    With a deadline, no request starts after it and none waits past it. httpx applies its timeout to
    each connect, write and read by itself, so a forge that sends a few bytes just inside every read
    could hold a call for as long as it liked. The request therefore runs in a thread of its own and
    is waited for until the time is up, and no longer. A request left behind like that stays the only
    one to its host: while it runs no other is started there, which is no try and is made up for in
    a later cycle. So a forge that does this costs one thread and one connection, not one per call,
    and no other forge's calls.

    The limit of it: a thread cannot be stopped from outside. For as long as a forge keeps such a
    response open, the markers and removals for that host wait, uncounted, and are made when it ends.
    """
    # One reading of the clock for both, so that nothing between them moves the cutoff past the deadline.
    now = time.monotonic()
    left = TIMEOUT_SECONDS if deadline is None else min(TIMEOUT_SECONDS, deadline - now)
    cutoff = now + left
    if left < SHORTEST_SECONDS:
        raise OutOfTime()  # Too little is as good as none: no request is worth starting for it.
    # One name for one host, however its URL is written: any case, the default port given or not.
    site = urlsplit(url)
    host = (site.hostname or "").lower(), site.port or 443
    if host in unanswered and unanswered[host].is_alive():
        raise Busy()
    unanswered.pop(host, None)
    answer = queue.SimpleQueue()
    # A forge that is only slow runs into httpx's own timeout, just before the wait here is over:
    # the error then names what was slow, and its thread has ended when the next call is made.
    patience = left - SHORTEST_SECONDS / 2

    # Whether the request is sent is settled once, between the thread and its caller: the thread
    # commits to sending only within the time and while it is still waited for, and the caller
    # that stops waiting learns whether it had. A call that sent nothing is no try.
    settle, state = threading.Lock(), {"sending": False, "waited_for": True}

    def send():
        try:
            with httpx.Client(timeout=patience, follow_redirects=False,
                              **(client_options() if project.provider == "gitlab" else {})) as client:
                with settle:  # After the client is ready, which can take its time: just before sending.
                    if time.monotonic() >= cutoff or not state["waited_for"]:
                        raise OutOfTime()
                    state["sending"] = True
                    started[0] += 1
                response = client.request(method, url, headers=headers, **options)
            # Said only when the client is closed, so that the next request to this host finds
            # nothing of this one still open, and a close that fails is not lost behind the answer.
            answer.put((response, None))
        except BaseException as err:
            answer.put((None, err))
    thread = threading.Thread(target=send, name="forge-marker-request", daemon=True)
    thread.start()
    try:
        response, error = answer.get(timeout=max(cutoff - time.monotonic(), 0))
    except queue.Empty:
        # Its thread is still running either way, be it only to prepare a client, and stays the
        # one for this host until it ends.
        unanswered[host] = thread
        with settle:
            state["waited_for"] = False
            if not state["sending"]:
                raise OutOfTime() from None  # It never got to send, and now will not.
        raise httpx.ReadTimeout("The forge did not answer in time") from None
    if error is not None:
        raise error
    return response


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
        if project.provider == "github" and deadline is not None:
            # Asking GitHub may just have written the reaction. Its removal is then not put off to a
            # later cycle, where a marker would appear on a request that is finished.
            deadline = max(deadline, time.monotonic() + START_SECONDS)
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


def notify(project, number, comment, state, deadline=None, command="/review"):
    # The helper publishes only on the change's current head. The notice is about the request, not a
    # head, so it goes on whatever head the change has now, once per head like any comment.
    left = HELPER_SECONDS if deadline is None else min(HELPER_SECONDS, deadline - time.monotonic())
    if left <= 0:
        raise OutOfTime()
    started[0] += 1
    head = run_forge("change", project, numbers=[number], timeout=left).get("headSha")
    if not valid_sha(head):
        raise ReviewError("Forge did not identify the change head")
    if deadline is not None and deadline <= time.monotonic():
        raise OutOfTime()
    # Started within the deadline and then never cut short: a helper stopped just after the forge
    # took the comment would leave no receipt, and the next try would post the notice again.
    request = f"{project.change_url(number)}{NOTE[project.provider]}{comment}"
    post_comment(project, number, head, NOTICE.format(command=command, request=request), state)


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

    def finished(self, project, number, comment, status, deadline=None, due=None):
        """Do what the finished request owes. With `due`, a time, only the parts whose next try has come."""
        job = project.key, number, comment
        self.marked.discard(job)
        said = job in self.refused
        self.refused.discard(job)
        owed = self.jobs.owed(job)
        if owed is None:
            return
        if deadline is None:
            deadline = time.monotonic() + FINISH_SECONDS

        def clear():
            if project.provider == "gitlab" and self.jobs.pending(project.key, number):
                # GitLab's marker stays while another request is waiting; its last request clears it.
                # Each of them gets a record for that, also one queued by a version that kept none.
                self.jobs.acknowledge_pending(project.key, number)
            else:
                remove(project, number, comment, deadline, said)

        def report_failure():
            # Named for what was asked: posting another command would start another kind of review.
            kind = self.jobs.tier(job)
            command = next((name for name, value in COMMANDS.items() if value == kind), "/review")
            notify(project, number, comment, self.state, deadline, command)

        # Kept apart: each is done, tried again and given up by itself, and while only one is owed
        # the other is left alone. The notice goes first and has the whole of the time. A removal
        # it leaves none for is not a try and is due at once, in a cycle where the notice is not;
        # the other way round, a slow removal would leave the notice a moment each time until
        # both were given up together.
        parts = (("notice_sent", report_failure, "report the failed review on", None, "failure notice", status == "failed"),
                 ("cleared", clear, "clear the queued marker on", comment, "removal of the queued marker", True))
        for part, action, verb, shown, name, wanted in parts:
            if owed[part] or not wanted:
                continue
            if due is not None and owed[part + "_retry_at"] > due:
                continue
            done = self.attempt(action, verb, project, number, shown)
            if done is False:
                tries = owed[part + "_tries"] + 1
                if tries < TRIES:
                    self.jobs.retry_acknowledgement(job, part, time.time() + min(RETRY_SECONDS * 2 ** (tries - 1), RETRY_CAP_SECONDS))
                    continue
                log.warning("Gave up on the %s of %s#%d comment %d after %d tries", name, project.slug, number, comment, tries)
            if done is not None:
                self.jobs.record_acknowledgement(job, part)
                owed[part] = 1
        if owed["cleared"] and (owed["notice_sent"] or status != "failed"):
            self.jobs.clear_acknowledgement(job)  # Also one a poller that stopped left with nothing owed.

    def settle(self, projects):
        """Try again what a finished request still owes the forge and is due: its marker's removal, its notice."""
        deadline = time.monotonic() + BUDGET_SECONDS
        now = time.time()
        for key, number, comment, status in self.jobs.unfinished_acknowledgements(now):
            if key in projects and time.monotonic() < deadline:
                self.finished(projects[key], number, comment, status, deadline, now)

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
        before = started[0]
        try:
            action()
            return True
        except OutOfTime as err:
            if started[0] == before:
                return None  # Not a failure, and not worth a line: it is done in a later cycle.
            # Part of it was done and the time ran out. That is a try, with the pause that follows one.
            log.warning("Could not %s %s#%d%s: %s", verb, project.slug, number,
                        f" comment {comment}" if comment else "", err)
            return False
        except Exception as err:
            # httpx errors quote the request URL. Report the status, or the class, and nothing else.
            detail = f"HTTP {err.response.status_code}" if isinstance(err, httpx.HTTPStatusError) else (
                     str(err) if isinstance(err, ReviewError) else type(err).__name__)
            log.warning("Could not %s %s#%d%s: %s", verb, project.slug, number,
                        f" comment {comment}" if comment else "", detail)
            return False
