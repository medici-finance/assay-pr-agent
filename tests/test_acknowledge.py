from contextlib import closing, nullcontext
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from cursor_pr import acknowledge, source
from cursor_pr.acknowledge import NOTICE, Acknowledgement
from cursor_pr.config import project_from
from cursor_pr.poll import Jobs, cycle, main
from cursor_pr.source import ReviewError

ME, OTHER = 42, 7
GITHUB = "https://api.github.com/repos/acme/github/issues/comments/81/reactions"
GITLAB = "https://gitlab.com/api/v4/projects/acme%2Fgitlab/merge_requests/3/award_emoji"
CLEAR = "Could not clear the queued marker on acme/github#3 comment 81: HTTP"


class Offline(unittest.TestCase):
    """Nothing here may reach a forge: an unmocked call fails instead of being sent."""

    def setUp(self):
        def connect(*args, **kwargs):
            raise AssertionError("This test tried to open a network connection")
        patcher = patch("socket.socket.connect", side_effect=connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()


class MarkerTests(Offline):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.gh, self.gl = [
            project_from(dict(provider=p, url=f"https://{p}.com/acme/{p}", requester_ids=[7],
                              checkout=tmp.name)) for p in ("github", "gitlab")]
        self.start(patch.dict(os.environ, {"GITLAB_API_BASE": "https://gitlab.com/api/v4"}))
        self.start(patch.object(acknowledge, "reviewer_token", return_value="fixture"))
        client = httpx.Client
        self.start(patch.object(acknowledge.httpx, "Client",
                                side_effect=lambda **opts: client(transport=httpx.MockTransport(self.respond), **opts)))
        self.calls = []
        self.refuse = {}
        self.eyes = [dict(id=11, user=dict(id=OTHER), content="eyes"), dict(id=13, user=dict(id=ME), content="heart")]
        self.awards = [dict(id=21, name="hourglass", user=dict(id=OTHER)), dict(id=23, name="fire", user=dict(id=ME))]
        self.account = dict(id=ME, login="reviewer-bot")

    def respond(self, request):
        self.calls.append((request.method, str(request.url), request.headers, request.content))
        url = str(request.url)
        status = self.refuse.get((request.method, url.split("?")[0]))
        if status:
            return httpx.Response(status, json=dict(message="details the log must not quote"), request=request)
        github = "api.github.com" in url
        if url.endswith("/user"):
            # A GitHub App installation token cannot read the account it acts as.
            return httpx.Response(403 if github else 200, json=self.account, request=request)
        rows, field = (self.eyes, "content") if github else (self.awards, "name")
        if request.method == "GET":
            wanted = request.url.params.get(field)
            return httpx.Response(200, json=[row for row in rows if not wanted or row[field] == wanted], request=request)
        if request.method == "POST" and github:
            # GitHub answers a reaction this account already has with 200 and that reaction.
            mine = [row for row in rows if row["user"]["id"] == ME and row["content"] == "eyes"]
            if mine:
                return httpx.Response(200, json=mine[0], request=request)
        if request.method == "POST":
            rows.append(dict(id=100 + len(self.calls), user=dict(id=ME), **{field: "eyes" if github else "hourglass"}))
            return httpx.Response(200 if github else 201, json=rows[-1], request=request)
        self.assertRegex(url, r"/(reactions|award_emoji)/[0-9]+$")  # Deleted by ID, never by a search.
        rows[:] = [row for row in rows if row["id"] != int(url.rsplit("/", 1)[1])]
        return httpx.Response(204, request=request)

    def sent(self):
        return [(method, url) for method, url, _, _ in self.calls]

    def check_headers(self, forge):
        for _, url, headers, _ in self.calls:
            self.assertEqual(url.split("/")[2], "api.github.com" if forge == "github" else "gitlab.com")
            self.assertEqual(headers["authorization" if forge == "github" else "private-token"],
                             "Bearer fixture" if forge == "github" else "fixture")
            self.assertNotIn("private-token" if forge == "github" else "authorization", headers)

    def ack(self):
        jobs = Jobs(self.state)
        for project in (self.gh, self.gl):
            for comment in (81, 82):
                jobs.enqueue(project.key, 3, comment, acknowledge=True)
        return Acknowledgement(jobs, self.state)

    def test_github_marker_uses_the_comment_reaction_endpoints(self):
        ack = self.ack()
        ack.queued(self.gh, 3, 81)
        self.assertEqual(self.calls, [("POST", GITHUB, self.calls[0][2], b'{"content":"eyes"}')])
        ack.finished(self.gh, 3, 81, "reviewed")
        self.assertEqual(self.sent(), [
            ("POST", GITHUB),
            ("POST", GITHUB),  # Asked for again: the answer names the reaction that is there.
            ("DELETE", GITHUB + "/101")])  # Not the other account's reaction, nor our own different one.
        self.check_headers("github")
        self.assertEqual([row["id"] for row in self.eyes], [11, 13])
        self.assertIsNone(ack.jobs.acknowledgement((self.gh.key, 3, 81)))

    def test_gitlab_marker_uses_the_merge_request_award(self):
        ack = self.ack()
        ack.queued(self.gl, 3, 81)
        self.assertEqual(self.sent(), [
            ("GET", "https://gitlab.com/api/v4/user"),
            ("GET", GITLAB + "?per_page=100&page=1"),
            ("POST", GITLAB + "?name=hourglass")])
        self.assertEqual(self.calls[-1][3], b"")
        while job := ack.jobs.next():
            ack.jobs.finish(job, "reviewed")
        ack.finished(self.gl, 3, 81, "reviewed")
        self.assertEqual(self.sent()[3:], [
            ("GET", "https://gitlab.com/api/v4/user"),
            ("GET", GITLAB + "?per_page=100&page=1"),
            ("DELETE", GITLAB + "/103")])
        self.check_headers("gitlab")
        self.assertEqual([row["id"] for row in self.awards], [21, 23])

    def test_one_marker_per_request_and_a_shared_award_is_added_once_and_removed_last(self):
        jobs = Jobs(self.state)
        ack = Acknowledgement(jobs, self.state)
        ack.queued(self.gh, 3, 81)
        ack.queued(self.gh, 3, 82)  # Another comment gets a reaction of its own.
        self.assertEqual(self.sent(), [("POST", GITHUB), ("POST", GITHUB.replace("comments/81/", "comments/82/"))])
        for comment in (81, 82):
            jobs.enqueue(self.gl.key, 3, comment, acknowledge=True)
            ack.queued(self.gl, 3, comment)
        self.assertEqual(self.sent()[2:], [
            ("GET", "https://gitlab.com/api/v4/user"), ("GET", GITLAB + "?per_page=100&page=1"),
            ("POST", GITLAB + "?name=hourglass"), ("GET", "https://gitlab.com/api/v4/user"),
            ("GET", GITLAB + "?per_page=100&page=1")])
        self.calls.clear()
        ack.finished(self.gl, 3, 81, "reviewed")
        self.assertEqual(self.calls, [])  # A request is still waiting on this merge request.
        for _ in range(2):
            jobs.finish(jobs.next(), "reviewed")  # Only a started request is finished.
        self.calls.clear()
        ack.finished(self.gl, 3, 82, "reviewed")
        self.assertEqual(self.sent(), [("GET", "https://gitlab.com/api/v4/user"),
                                       ("GET", GITLAB + "?per_page=100&page=1"), ("DELETE", GITLAB + "/105")])
        self.assertEqual([row["id"] for row in self.awards], [21, 23])

    def test_marker_refused_for_good_is_not_asked_for_again_and_its_removal_says_nothing(self):
        ack = self.ack()
        self.refuse = {("POST", GITHUB): 403}  # As without the Issues permission.
        with self.assertLogs("cursor_pr.acknowledge", level="WARNING") as logged:
            ack.queued(self.gh, 3, 81)
            ack.starting(self.gh, 3, 81)
            # The removal asks, in case an earlier poller did mark it, and is refused the same way.
            ack.finished(self.gh, 3, 81, "reviewed")
        self.assertEqual(self.sent(), [("POST", GITHUB), ("POST", GITHUB)])
        self.assertEqual(len(logged.output), 1)  # One line per request, from the marking.
        self.assertIsNone(ack.jobs.acknowledgement((self.gh.key, 3, 81)))

    def test_no_time_left_is_not_a_failed_try(self):
        ack = self.ack()
        job = self.gh.key, 3, 81
        ack.jobs.next()
        ack.jobs.finish(job, "failed")
        with self.assertNoLogs("cursor_pr.acknowledge", level="WARNING"):
            ack.finished(self.gh, 3, 81, "failed", acknowledge.time.monotonic() - 1)
        self.assertEqual(self.calls, [])
        self.assertEqual((ack.jobs.acknowledgement(job), ack.jobs.acknowledgement_tries(job)), ((0, 0), 0))
        self.assertIn((*job, "failed"), ack.jobs.unfinished_acknowledgements(acknowledge.time.time()))  # Still due.

    def test_marker_turned_away_for_now_is_asked_for_again_when_the_review_starts(self):
        ack = self.ack()
        for refuse in ({("POST", GITHUB): 429}, {("POST", GITHUB): 502}):
            self.refuse = refuse
            with self.assertLogs("cursor_pr.acknowledge", level="WARNING"):
                ack.queued(self.gh, 3, 81)
        self.refuse = {}
        ack.starting(self.gh, 3, 81)
        self.assertEqual(len(self.eyes), 3)
        self.calls.clear()
        ack.starting(self.gh, 3, 81)  # Marked now.
        self.assertEqual(self.calls, [])

    def test_reaction_this_process_knows_nothing_about_is_found_and_removed(self):
        # GitHub made the reaction and the answer never arrived, or the poller stopped right after.
        ack = self.ack()
        respond = self.respond

        def timed_out(request):
            respond(request)
            raise httpx.ReadTimeout("no answer", request=request)
        with patch.object(self, "respond", timed_out), self.assertLogs("cursor_pr.acknowledge", level="WARNING"):
            ack.queued(self.gh, 3, 81)
        self.assertEqual(len(self.eyes), 3)
        Acknowledgement(Jobs(self.state), self.state).finished(self.gh, 3, 81, "reviewed")  # After a restart.
        self.assertEqual([row["id"] for row in self.eyes], [11, 13])
        self.assertIsNone(Jobs(self.state).acknowledgement((self.gh.key, 3, 81)))

    def test_removal_stays_owed_while_the_forge_may_still_hold_the_marker(self):
        job = self.gh.key, 3, 81
        ack = self.ack()
        ack.queued(self.gh, 3, 81)
        for status, headers in ((502, {}), (429, {}), (403, {"retry-after": "60"}), (403, {"x-ratelimit-remaining": "0"})):
            with self.subTest(status=status, headers=headers):
                limited = lambda request: httpx.Response(status, headers=headers, json={}, request=request)
                with patch.object(self, "respond", limited), self.assertLogs("cursor_pr.acknowledge", level="WARNING"):
                    ack.finished(self.gh, 3, 81, "reviewed")
                self.assertEqual(ack.jobs.acknowledgement(job), (0, 0))
        ack.finished(self.gh, 3, 81, "reviewed")
        self.assertEqual([row["id"] for row in self.eyes], [11, 13])
        self.assertIsNone(ack.jobs.acknowledgement(job))

    def test_removal_refused_for_good_is_not_owed_any_longer_and_says_what_may_be_left(self):
        cases = [(self.gh, {("DELETE", GITHUB + "/101"): 403}, "HTTP 403"), (self.gh, {("DELETE", GITHUB + "/101"): 301}, "HTTP 301"),
                 (self.gh, {("DELETE", GITHUB + "/101"): 404}, None), (self.gl, {("GET", GITLAB): 404}, None),
                 (self.gl, {("GET", "https://gitlab.com/api/v4/user"): 403}, "HTTP 403")]
        for project, refuse, said in cases:
            with self.subTest(refuse=refuse):
                self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
                self.calls.clear()
                self.eyes[:] = self.eyes[:2]
                ack = self.ack()
                job = project.key, 3, 81
                ack.queued(project, 3, 81)
                while started := ack.jobs.next():
                    ack.jobs.finish(started, "reviewed")
                self.refuse = refuse
                with self.assertLogs("cursor_pr.acknowledge", level="WARNING") as logged:
                    logging.getLogger("cursor_pr.acknowledge").warning("end of what was logged")
                    ack.finished(project, 3, 81, "reviewed")
                self.assertEqual([line.split(": ", 1)[1] for line in logged.output[1:]],
                                 [said] if said else [])
                self.assertIsNone(ack.jobs.acknowledgement(job))
                self.refuse = {}

    def test_marker_is_left_alone_while_only_the_notice_is_owed(self):
        ack = self.ack()
        ack.queued(self.gh, 3, 81)
        with patch.object(acknowledge, "notify", side_effect=ReviewError("change is no longer open")), \
                self.assertLogs("cursor_pr.acknowledge", level="WARNING"):
            ack.finished(self.gh, 3, 81, "failed")  # The marker goes; the notice fails.
            self.assertEqual(self.sent(), [("POST", GITHUB), ("POST", GITHUB), ("DELETE", GITHUB + "/101")])
            self.assertEqual(ack.jobs.acknowledgement((self.gh.key, 3, 81)), (0, 1))
            ack.finished(self.gh, 3, 81, "failed")  # Only the notice is tried again: no reaction is asked for.
        self.assertEqual(len(self.sent()), 3)
        self.assertEqual([row["id"] for row in self.eyes], [11, 13])

    def test_gitlab_award_is_removed_by_a_waiting_request_that_had_no_record(self):
        jobs = Jobs(self.state)
        jobs.enqueue(self.gl.key, 3, 80)  # Queued by a version that kept no acknowledgements.
        jobs.enqueue(self.gl.key, 3, 81, acknowledge=True)
        ack = Acknowledgement(jobs, self.state)
        ack.queued(self.gl, 3, 81)
        mine = lambda: [row for row in self.awards if row["user"]["id"] == ME and row["name"] == "hourglass"]
        self.assertEqual(len(mine()), 1)
        for comment in (81, 80):  # The newer request finishes first, as when the older one was resumed.
            with closing(sqlite3.connect(self.state / "requests.sqlite3")) as db, db:
                db.execute("UPDATE requests SET status='reviewed' WHERE comment=?", (comment,))
            ack.finished(self.gl, 3, comment, "reviewed")
            self.assertEqual(len(mine()), 1 if comment == 81 else 0)
        self.assertEqual(jobs.unfinished_acknowledgements(), [])

    def test_a_forge_that_does_not_answer_holds_the_queue_back_only_so_long(self):
        jobs = Jobs(self.state)
        for comment in range(81, 121):
            jobs.enqueue(self.gh.key, 3, comment, acknowledge=True)
        clock, waits = [1000.0], []
        with httpx.Client() as made:  # setUp replaced the name; this is the class behind it.
            client = type(made)

        def silent(**options):
            def wait(request):
                waits.append(options["timeout"])
                clock[0] += options["timeout"]  # The forge lets every request run into its timeout.
                raise httpx.ReadTimeout("no answer", request=request)
            return client(transport=httpx.MockTransport(wait), **options)
        ack = Acknowledgement(jobs, self.state)
        with patch.object(acknowledge.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(acknowledge.httpx, "Client", side_effect=silent), \
                self.assertLogs("cursor_pr.acknowledge", level="WARNING"):
            ack.mark({self.gh.key: self.gh})
            self.assertEqual((waits, clock[0] - 1000), ([15, 5], 20))  # The pass's budget, whatever the requests take.
            # A request the pass did not get to, or got no answer for, has a short try when its review starts.
            del waits[:]
            ack.starting(self.gh, 3, 83)
            self.assertEqual(waits, [5])
            # Its removal and its notice right after the review share one limit as well.
            del waits[:]
            before = clock[0]
            jobs.next()
            jobs.finish((self.gh.key, 3, 81), "failed")
            helper = []

            def slow(verb, project, *, numbers=(), timeout=45):
                helper.append((verb, timeout))
                clock[0] += timeout
                raise subprocess.TimeoutExpired(verb, timeout)
            with patch.object(acknowledge, "run_forge", side_effect=slow), \
                    patch.object(acknowledge, "post_comment", side_effect=lambda *args: helper.append("comment")):
                ack.finished(self.gh, 3, 81, "failed")
            # The removal takes its timeout, the read of the change what is left, and no comment is started.
            self.assertEqual((waits, helper, clock[0] - before), ([15], [("change", 5)], 20))
            self.assertEqual(jobs.acknowledgement((self.gh.key, 3, 81)), (0, 0))  # Both still owed, for a later cycle.
            # And so does the pass over what earlier requests still owe.
            for comment in (82, 83, 84):
                jobs.next()
                jobs.finish((self.gh.key, 3, comment), "reviewed")
            del waits[:]
            before = clock[0]
            clock[0] += 0  # Rows are due at once: none was tried yet, except the first, which waits a minute.
            ack.settle({self.gh.key: self.gh})
            self.assertEqual((waits, clock[0] - before), ([15, 5], 20))

    def test_cycle_marks_before_the_reviews_and_again_at_a_review_the_pass_missed(self):
        jobs = Jobs(self.state)
        for comment in (81, 82):
            jobs.enqueue(self.gh.key, 3, comment, acknowledge=True)  # Queued by a poller that stopped before marking.
        ack = Acknowledgement(Jobs(self.state), self.state)
        other = GITHUB.replace("/81/", "/82/")
        seen = []

        def review(project, number, state):
            seen.append([url for method, url in self.sent() if method == "POST"])
            return "reviewed"
        with patch("cursor_pr.poll.poll_project"):
            cycle({self.gh.key: self.gh}, self.state, ack.jobs, {self.gh.key: None}, review, ack=ack)
        self.assertEqual(seen[0], [GITHUB, other])  # Both were marked before the first review started,
        self.assertEqual(seen[1], [GITHUB, other, GITHUB])  # and not a second time when their own did.
        self.assertEqual([method for method, _ in self.sent()].count("DELETE"), 2)
        for comment in (83, 84):
            jobs.enqueue(self.gh.key, 3, comment, acknowledge=True)
        self.calls.clear()
        del seen[:]
        with patch.object(ack, "mark"), patch("cursor_pr.poll.poll_project"):  # The pass ran out of time.
            cycle({self.gh.key: self.gh}, self.state, ack.jobs, {self.gh.key: None}, review, ack=ack)
        self.assertEqual(seen[0], [GITHUB.replace("/81/", "/83/")])  # Each review still ran with its request marked.
        self.assertEqual(seen[1][-1], GITHUB.replace("/81/", "/84/"))
        self.assertEqual([row["id"] for row in self.eyes], [11, 13])

    def test_requests_waiting_since_before_the_upgrade_are_marked_and_told_when_they_fail(self):
        with closing(sqlite3.connect(self.state / "requests.sqlite3")) as db, db:
            db.execute("""CREATE TABLE requests (project TEXT NOT NULL, number INTEGER NOT NULL, comment INTEGER NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, head TEXT NOT NULL DEFAULT '', prepared TEXT,
                PRIMARY KEY(project, number, comment))""")
            db.executemany("INSERT INTO requests (project, number, comment, status) VALUES (?,?,?,?)",
                           [(self.gh.key, 3, 81, "queued"), (self.gh.key, 3, 80, "reviewed")])
        jobs = Jobs(self.state)
        self.assertEqual(jobs.acknowledgement((self.gh.key, 3, 81)), (0, 0))
        self.assertIsNone(jobs.acknowledgement((self.gh.key, 3, 80)))
        self.assertEqual(jobs.waiting_acknowledgements(), [(self.gh.key, 3, 81)])
        jobs.enqueue(self.gh.key, 3, 82)
        self.assertIsNone(Jobs(self.state).acknowledgement((self.gh.key, 3, 82)))  # Only at the upgrade.
        jobs.next()
        jobs.finish((self.gh.key, 3, 81), "failed")
        with patch.object(acknowledge, "notify") as notify, patch.object(acknowledge, "remove"):
            Acknowledgement(jobs, self.state).finished(self.gh, 3, 81, "failed")
        self.assertEqual(notify.call_args.args[:4], (self.gh, 3, 81, self.state))

    def test_table_of_a_version_that_kept_marker_ids_goes_on_working(self):
        Jobs(self.state)
        with closing(sqlite3.connect(self.state / "requests.sqlite3")) as db, db:
            db.execute("DROP TABLE acknowledgements")
            db.execute("""CREATE TABLE acknowledgements (project TEXT NOT NULL, number INTEGER NOT NULL,
                comment INTEGER NOT NULL, marker_id INTEGER, notice_sent INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(project, number, comment))""")
        ack = self.ack()
        ack.queued(self.gh, 3, 81)
        ack.finished(self.gh, 3, 81, "reviewed")
        self.assertEqual([row["id"] for row in self.eyes], [11, 13])
        self.assertIsNone(ack.jobs.acknowledgement((self.gh.key, 3, 81)))

    def test_gitlab_marker_lookup_stops_on_the_matching_award(self):
        ack = self.ack()
        self.awards.extend([dict(id=201, name="hourglass", user=dict(id=ME))] +
                           [dict(id=i, name="hourglass", user=dict(id=OTHER)) for i in range(202, 299)])
        while job := ack.jobs.next():
            ack.jobs.finish(job, "reviewed")
        ack.finished(self.gl, 3, 81, "reviewed")
        self.assertEqual(self.sent(), [("GET", "https://gitlab.com/api/v4/user"),
                                       ("GET", GITLAB + "?per_page=100&page=1"), ("DELETE", GITLAB + "/201")])

    def test_failures_are_logged_without_the_url_or_token_and_never_raised(self):
        def finished(project, number, comment):
            ack = self.ack()
            while job := ack.jobs.next():
                ack.jobs.finish(job, "reviewed")
            # The reviewer's own marker is there to be removed, beside everyone else's.
            self.eyes[:] = [dict(id=11, user=dict(id=OTHER), content="eyes"),
                            dict(id=12, user=dict(id=ME), content="eyes"), dict(id=13, user=dict(id=ME), content="heart")]
            self.awards[:] = [dict(id=21, name="hourglass", user=dict(id=OTHER)),
                              dict(id=22, name="hourglass", user=dict(id=ME)), dict(id=23, name="fire", user=dict(id=ME))]
            ack.finished(project, number, comment, "reviewed")
        ack = self.ack()
        cases = [({("POST", GITHUB): 403}, (self.gh, 3, 81), ack.queued, "Could not mark acme/github#3 comment 81: HTTP 403"),
                 ({("GET", GITLAB): 403}, (self.gl, 3, 81), ack.queued, "Could not mark acme/gitlab#3 comment 81: HTTP 403"),
                 ({("POST", GITLAB): 404}, (self.gl, 3, 81), ack.queued, "Could not mark acme/gitlab#3 comment 81: HTTP 404"),
                 ({("DELETE", GITHUB + "/12"): 401}, (self.gh, 3, 81), finished, CLEAR + " 401"),
                 ({("GET", "https://gitlab.com/api/v4/user"): 502}, (self.gl, 3, 81), finished, "Could not clear the queued marker on acme/gitlab#3 comment 81: HTTP 502")]
        for refuse, (project, number, comment), call, line in cases:
            with self.subTest(line=line):
                self.refuse = refuse
                self.calls.clear()
                with self.assertLogs("cursor_pr.acknowledge", level="WARNING") as logged:
                    call(project, number, comment)
                self.assertEqual(logged.output, ["WARNING:cursor_pr.acknowledge:" + line])
                for secret in ("https://", "fixture", "details the log must not quote"):
                    self.assertNotIn(secret, logged.output[0])
        self.refuse = {("DELETE", GITHUB + "/12"): 500}
        with self.assertLogs("cursor_pr.acknowledge", level="WARNING") as logged:
            finished(self.gh, 3, 81)
        self.assertEqual(logged.output, ["WARNING:cursor_pr.acknowledge:" + CLEAR + " 500"])
        self.refuse = {("DELETE", GITLAB + "/22"): 404}
        with self.assertNoLogs("cursor_pr.acknowledge", level="WARNING"):
            finished(self.gl, 3, 81)  # Already gone.
        self.refuse = {}
        for problem, line in [(ReviewError("desktoken --no-rotate exited 5"), "desktoken --no-rotate exited 5"),
                              (httpx.ConnectError("secret path"), "ConnectError"),
                              (OSError("secret path"), "OSError")]:
            with self.subTest(line=line), patch.object(acknowledge, "reviewer_token", side_effect=problem), \
                 self.assertLogs("cursor_pr.acknowledge", level="WARNING") as logged:
                ack.queued(self.gh, 3, 81)
            self.assertEqual(logged.output, [f"WARNING:cursor_pr.acknowledge:Could not mark acme/github#3 comment 81: {line}"])
        with patch.dict(os.environ, {"GITLAB_API_BASE": "https://elsewhere.example/api/v4"}), \
             patch.object(acknowledge, "reviewer_token") as lookup, \
             self.assertLogs("cursor_pr.acknowledge", level="WARNING") as logged:
            ack.queued(self.gl, 3, 81)
        lookup.assert_not_called()
        self.assertIn("GITLAB_API_BASE must match", logged.output[0])
        self.calls.clear()
        for number, comment in ((3, "81"), (0, 81), (3, True)):
            with self.assertLogs("cursor_pr.acknowledge", level="WARNING"):
                ack.queued(self.gh, number, comment)  # Only real IDs become part of a URL.
        self.assertEqual(self.calls, [])


class NoticeTests(Offline):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.gh, self.gl = [
            project_from(dict(provider=p, url=f"https://{p}.com/acme/{p}", requester_ids=[7],
                              checkout=tmp.name)) for p in ("github", "gitlab")]
        self.projects = {p.key: p for p in (self.gh, self.gl)}
        for name in ("reviewer_token", "run_forge", "post_comment", "add", "remove"):
            self.start(patch.object(acknowledge, name, side_effect=AssertionError(name + " ran in a test")))

    def status(self, project, number, comment):
        with sqlite3.connect(self.state / "requests.sqlite3") as db:
            return db.execute("SELECT status FROM requests WHERE project=? AND number=? AND comment=?",
                              (project, number, comment)).fetchone()[0]

    def test_failed_review_posts_one_fixed_comment_on_the_current_head_and_nothing_else(self):
        jobs = Jobs(self.state)
        for project in (self.gh, self.gl):
            jobs.enqueue(project.key, 3, 81, acknowledge=True)
        ack = Acknowledgement(jobs, self.state)
        head = "a" * 40
        with patch.object(acknowledge, "run_forge", return_value=dict(headSha=head)) as read, \
                patch.object(acknowledge, "post_comment") as post, patch.object(acknowledge, "remove"):
            ack.finished(self.gh, 3, 81, "failed")
            jobs.enqueue(self.gh.key, 3, 82, acknowledge=True)
            for status in ("reviewed", "partial", "empty", "over-budget", "model-unavailable"):
                ack.finished(self.gh, 3, 82, status)
            self.assertEqual(read.call_count, 1)
            self.assertLessEqual(read.call_args.kwargs.pop("timeout"), 45)
            read.assert_called_once_with("change", self.gh, numbers=[3])
            post.assert_called_once_with(self.gh, 3, head, NOTICE.format(request=self.gh.change_url(3) + "#issuecomment-81"),
                                         self.state)
            post.side_effect = ReviewError("pr-agent-forge comment exited 1: change is no longer open")
            with self.assertLogs("cursor_pr.acknowledge", level="WARNING") as logged:
                ack.finished(self.gl, 3, 81, "failed")
            self.assertEqual(post.call_args.args[:3], (self.gl, 3, head))
            self.assertEqual(logged.output, ["WARNING:cursor_pr.acknowledge:Could not report the failed review on "
                                             "acme/gitlab#3: pr-agent-forge comment exited 1: change is no longer open"])
            post.reset_mock()
            read.return_value = dict(headSha="not a head")
            jobs.enqueue(self.gh.key, 3, 83, acknowledge=True)
            with self.assertLogs("cursor_pr.acknowledge", level="WARNING") as logged:
                ack.finished(self.gh, 3, 83, "failed")
            post.assert_not_called()
            self.assertIn("Forge did not identify the change head", logged.output[0])

    def test_failure_notice_goes_to_the_forge_helper_as_a_head_bound_comment(self):
        # The real path below the notice, with only the helper process and the credential replaced.
        head, sent = "b" * 40, []

        def helper(args, *, data=None, env=None, timeout=45, cwd=None):
            sent.append((args, json.loads(data)))
            return json.dumps(dict(headSha=head) if args[1] == "change" else None).encode()
        with patch.object(acknowledge, "run_forge", side_effect=source.run_forge), \
                patch.object(acknowledge, "post_comment", side_effect=source.post_comment), \
                patch.object(source, "reviewer_token", return_value="fixture"), \
                patch.object(source, "run", side_effect=helper):
            acknowledge.notify(self.gh, 3, 81, self.state)
            acknowledge.notify(self.gh, 3, 81, self.state)  # Once per head: the second is not posted again.
            acknowledge.notify(self.gh, 3, 82, self.state)  # Another request that failed at this head has its own.
        self.assertEqual([(args, request["Numbers"]) for args, request in sent[::2]],
                         [(["pr-agent-forge", "change"], [3])] * 3)
        self.assertEqual([args for args, _ in sent[1::2]], [["pr-agent-forge", "comment"]] * 3)
        comments = [request["Publication"] for _, request in sent[1::2]]
        self.assertEqual([(c["Number"], c["Head"], c["Body"], c["AlreadyPosted"]) for c in comments],
                         [(3, head, NOTICE.format(request=self.gh.change_url(3) + f"#issuecomment-{comment}"), posted)
                          for comment, posted in ((81, False), (81, True), (82, False))])
        self.assertIn("#note_81", NOTICE.format(request=self.gl.change_url(3) + acknowledge.NOTE["gitlab"] + "81"))

    def test_cycle_marks_each_new_request_once_and_reports_only_a_failed_review(self):
        jobs = Jobs(self.state)
        since = {key: datetime(2026, 11, 15, tzinfo=timezone.utc) for key in self.projects}
        ack, threads, outcomes, public, results = Mock(jobs=jobs, marked=set()), {p.slug: [] for p in self.projects.values()}, [], [True], []
        ack.mark.side_effect = lambda projects: Acknowledgement.mark(ack, projects)  # The real choice of what to mark.
        def forge(verb, project, *, numbers=(), timeout=45):
            if verb == "open":
                return dict(numbers=[3], capped=False)
            self.assertEqual((verb, numbers), ("comments", (3,)))
            return dict(partial=[], items=[dict(number=3, comments=threads[project.slug])])
        request = dict(authorId=7, databaseId=81, authorType="User", body="/review",
                       createdAt="2026-11-15T11:11:00Z", updatedAt="2026-11-15T11:11:00Z",
                       url=self.gh.change_url(3) + "#issuecomment-81")
        def review(project, number, state):
            outcomes.append((project.slug, number))
            return results.pop(0)
        with patch("cursor_pr.discovery.run_forge", side_effect=forge), \
             patch("cursor_pr.discovery.public_gitlab_request", side_effect=lambda *args: public[0]):
            for slug in threads:
                threads[slug].append(request | dict(body="ordinary comment"))
            cycle(self.projects, self.state, jobs, since, review, ack)
            ack.queued.assert_not_called()
            public[0] = False
            threads[self.gl.slug].append(request)
            cycle(self.projects, self.state, jobs, since, review, ack)  # An internal note is never marked.
            ack.queued.assert_not_called()
            self.assertEqual(self.status(self.gl.key, 3, 81), "ignored")
            public[0] = True
            threads[self.gl.slug].append(request | dict(databaseId=82))
            threads[self.gh.slug].append(request | dict(databaseId=83, url=self.gh.change_url(3) + "#issuecomment-83"))
            results[:] = ["reviewed", "failed"]
            cycle(self.projects, self.state, jobs, since, review, ack)
            self.assertEqual([call.args[:3] for call in ack.queued.call_args_list],
                             [(self.gh, 3, 83), (self.gl, 3, 82)])
            self.assertEqual(outcomes, [("acme/github", 3), ("acme/gitlab", 3)])
            self.assertEqual([call.args for call in ack.finished.call_args_list],
                             [(self.gh, 3, 83, "reviewed"), (self.gl, 3, 82, "failed")])
            cycle(self.projects, self.state, jobs, since, review, ack)  # Already recorded: no second marker.
            self.assertEqual((ack.queued.call_count, ack.finished.call_count), (2, 2))

    def test_cycle_reports_how_the_row_says_the_review_ended(self):
        # The review child settles its own row, then its report is lost: it posted, so no notice.
        jobs, ack = Jobs(self.state), Mock()
        jobs.enqueue(self.gh.key, 3, 81)

        def review(project, number, state):
            with closing(sqlite3.connect(self.state / "requests.sqlite3")) as db, db:
                db.execute("UPDATE requests SET status='reviewed' WHERE status='started'")
            return "failed"
        with patch("cursor_pr.poll.poll_project"):
            cycle(self.projects, self.state, jobs, dict.fromkeys(self.projects), review, ack)
        ack.finished.assert_called_once_with(self.gh, 3, 81, "reviewed")

    def test_restart_cleans_up_terminal_requests_without_rerunning_the_review(self):
        jobs = Jobs(self.state)
        for comment, status in ((81, "reviewed"), (82, "failed")):
            job = (self.gh.key, 3, comment)
            jobs.enqueue(*job, acknowledge=True)
            self.assertEqual(jobs.next(), job)
            jobs.finish(job, status)

        jobs = Jobs(self.state)
        self.assertIsNone(jobs.next())
        ack = Acknowledgement(jobs, self.state)
        with patch.object(acknowledge, "remove") as remove, patch.object(acknowledge, "notify") as notify:
            ack.settle(self.projects)
        self.assertEqual([call.args[:3] for call in remove.call_args_list], [(self.gh, 3, 81), (self.gh, 3, 82)])
        self.assertEqual([call.args[:4] for call in notify.call_args_list], [(self.gh, 3, 82, self.state)])
        self.assertEqual(jobs.unfinished_acknowledgements(), [])

    def test_running_poller_tries_again_with_growing_pauses_and_gives_up(self):
        jobs = Jobs(self.state)
        job = (self.gh.key, 3, 81)
        jobs.enqueue(*job, acknowledge=True)
        jobs.next()
        now = [1_800_000_000.0]
        tried = []
        self.start(patch.object(acknowledge.time, "time", side_effect=lambda: now[0]))

        def down(*args):
            tried.append(now[0] - 1_800_000_000)
            raise httpx.ConnectError("down")
        with patch.object(acknowledge, "remove", side_effect=down), patch("cursor_pr.poll.poll_project"), \
                self.assertLogs("cursor_pr.acknowledge", level="WARNING") as logged:
            ack = Acknowledgement(jobs, self.state)
            projects, since = {self.gh.key: self.gh}, {self.gh.key: datetime(2026, 1, 1, tzinfo=timezone.utc)}
            jobs.finish(job, "reviewed")
            ack.finished(self.gh, 3, 81, "reviewed")  # The review ends; the removal fails.
            for _ in range(600):  # Ten hours of cycles in a poller that never restarts.
                now[0] += 60
                cycle(projects, self.state, jobs, since, ack=ack)
        self.assertEqual(tried[:8], [0, 60, 180, 420, 900, 1860, 3780, 7380])  # 1, 2, 4, 8, 16, 32 and then 60 minutes.
        self.assertEqual(len(tried), acknowledge.TRIES)
        self.assertIn("Gave up on the queued marker or failure notice of acme/github#3 comment 81 after 12 tries", logged.output[-1])
        self.assertIsNone(jobs.acknowledgement(job))

    def test_restart_tells_the_requester_about_a_review_it_gave_up_on(self):
        jobs = Jobs(self.state)
        jobs.enqueue(self.gh.key, 3, 81)
        jobs.next()
        (self.state / "projects.json").write_text("{}")
        with self.assertLogs("cursor_pr.poll", level="WARNING"):
            Jobs(self.state)
            Jobs(self.state).next()  # Interrupted twice; the next start gives up on it.
        watch = Mock(enabled=False)
        watch.changed.side_effect = [False, True]
        with patch("sys.argv", ["cursor-review-poll", "--state-dir", str(self.state)]), \
             patch("cursor_pr.poll.ConfigurationWatch", return_value=watch), \
             patch("cursor_pr.poll.setup.apply_saved_setup"), \
             patch("cursor_pr.poll.providers.provider", return_value="cursor"), \
             patch("cursor_pr.poll.auth_mode", return_value="api-key"), \
             patch("cursor_pr.poll.setup.max_input_tokens", return_value=6000), \
             patch("cursor_pr.poll.history.interval", return_value=0), \
             patch("cursor_pr.poll.load_config", return_value={self.gh.key: self.gh}), \
             patch("cursor_pr.poll.providers.cursor_account", side_effect=lambda: nullcontext()), \
             patch("cursor_pr.poll.forge_credentials", side_effect=lambda *args: nullcontext()), \
             patch("cursor_pr.poll.poll_project") as polled, patch("cursor_pr.poll.logging.basicConfig"), \
             patch.object(acknowledge, "remove"), patch.object(acknowledge, "notify") as notify, \
             self.assertLogs("cursor_pr.poll", level="WARNING"):
            main()
        self.assertEqual([call.args[:4] for call in notify.call_args_list], [(self.gh, 3, 81, self.state.resolve())])
        self.assertIsNotNone(polled.call_args.args[3])  # The running poller marks too.
        reopened = Jobs(self.state)
        self.assertEqual((reopened.unfinished_acknowledgements(), reopened.pending(self.gh.key, 3)), ([], False))

    def test_restart_gives_up_on_a_review_whose_head_was_not_recorded(self):
        # A queue from a version that kept no head: its running review cannot be repeated safely.
        with closing(sqlite3.connect(self.state / "requests.sqlite3")) as db, db:
            db.execute("""CREATE TABLE requests (project TEXT NOT NULL, number INTEGER NOT NULL,
                comment INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(project, number, comment))""")
            db.execute("INSERT INTO requests VALUES (?, 3, 81, 'started', 1)", (self.gh.key,))
        with self.assertLogs("cursor_pr.poll", level="WARNING"):
            jobs = Jobs(self.state)
        # It owes its requester the notice, like any review that failed.
        self.assertEqual(jobs.acknowledgement((self.gh.key, 3, 81)), (0, 0))
        self.assertEqual(jobs.unfinished_acknowledgements(), [(self.gh.key, 3, 81, "failed")])


if __name__ == "__main__":
    unittest.main()
