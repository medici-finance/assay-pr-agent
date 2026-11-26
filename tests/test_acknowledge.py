from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from cursor_pr import acknowledge
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
        if url.endswith("/user"):
            return httpx.Response(200, json=self.account, request=request)
        github = "api.github.com" in url
        rows, field = (self.eyes, "content") if github else (self.awards, "name")
        if request.method == "GET":
            wanted = request.url.params.get(field)
            return httpx.Response(200, json=[row for row in rows if not wanted or row[field] == wanted], request=request)
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

    def test_github_marker_uses_the_comment_reaction_endpoints(self):
        ack = Acknowledgement(Jobs(self.state))
        ack.queued(self.gh, 3, 81)
        self.assertEqual(self.calls, [("POST", GITHUB, self.calls[0][2], b'{"content":"eyes"}')])
        ack.finished(self.gh, 3, 81, "reviewed")
        self.assertEqual(self.sent(), [
            ("POST", GITHUB),
            ("GET", "https://api.github.com/user"),
            ("GET", GITHUB + "?per_page=100&page=1&content=eyes"),
            ("DELETE", GITHUB + "/101")])  # Not the other account's reaction, nor our own different one.
        self.check_headers("github")
        self.assertEqual([row["id"] for row in self.eyes], [11, 13])

    def test_gitlab_marker_uses_the_merge_request_award(self):
        ack = Acknowledgement(Jobs(self.state))
        ack.queued(self.gl, 3, 81)
        self.assertEqual(self.sent(), [
            ("GET", "https://gitlab.com/api/v4/user"),
            ("GET", GITLAB + "?per_page=100&page=1"),
            ("POST", GITLAB + "?name=hourglass")])
        self.assertEqual(self.calls[-1][3], b"")
        ack.finished(self.gl, 3, 81, "reviewed")
        self.assertEqual(self.sent()[3:], [
            ("GET", "https://gitlab.com/api/v4/user"),
            ("GET", GITLAB + "?per_page=100&page=1"),
            ("DELETE", GITLAB + "/103")])
        self.check_headers("gitlab")
        self.assertEqual([row["id"] for row in self.awards], [21, 23])

    def test_one_marker_per_request_and_a_shared_award_is_added_once_and_removed_last(self):
        jobs = Jobs(self.state)
        ack = Acknowledgement(jobs)
        ack.queued(self.gh, 3, 81)
        ack.queued(self.gh, 3, 82)  # Another comment gets a reaction of its own.
        self.assertEqual(self.sent(), [("POST", GITHUB), ("POST", GITHUB.replace("comments/81/", "comments/82/"))])
        for comment in (81, 82):
            jobs.enqueue(self.gl.key, 3, comment)
            ack.queued(self.gl, 3, comment)
        self.assertEqual(self.sent()[2:], [
            ("GET", "https://gitlab.com/api/v4/user"), ("GET", GITLAB + "?per_page=100&page=1"),
            ("POST", GITLAB + "?name=hourglass"), ("GET", "https://gitlab.com/api/v4/user"),
            ("GET", GITLAB + "?per_page=100&page=1")])
        self.calls.clear()
        ack.finished(self.gl, 3, 81, "reviewed")
        self.assertEqual(self.calls, [])  # A request is still waiting on this merge request.
        jobs.finish((self.gl.key, 3, 81), "reviewed")
        jobs.finish((self.gl.key, 3, 82), "reviewed")
        self.calls.clear()
        ack.finished(self.gl, 3, 82, "reviewed")
        self.assertEqual(self.sent(), [("GET", "https://gitlab.com/api/v4/user"),
                                       ("GET", GITLAB + "?per_page=100&page=1"), ("DELETE", GITLAB + "/105")])
        self.assertEqual([row["id"] for row in self.awards], [21, 23])

    def test_failures_are_logged_without_the_url_or_token_and_never_raised(self):
        ack = Acknowledgement(Jobs(self.state))
        def finished(project, number, comment):
            # The reviewer's own marker is there to be removed, beside everyone else's.
            self.eyes[:] = [dict(id=11, user=dict(id=OTHER), content="eyes"),
                            dict(id=12, user=dict(id=ME), content="eyes"), dict(id=13, user=dict(id=ME), content="heart")]
            self.awards[:] = [dict(id=21, name="hourglass", user=dict(id=OTHER)),
                              dict(id=22, name="hourglass", user=dict(id=ME)), dict(id=23, name="fire", user=dict(id=ME))]
            ack.finished(project, number, comment, "reviewed")
        cases = [({("POST", GITHUB): 403}, (self.gh, 3, 81), ack.queued, "Could not mark acme/github#3 comment 81: HTTP 403"),
                 ({("GET", GITLAB): 403}, (self.gl, 3, 81), ack.queued, "Could not mark acme/gitlab#3 comment 81: HTTP 403"),
                 ({("POST", GITLAB): 404}, (self.gl, 3, 81), ack.queued, "Could not mark acme/gitlab#3 comment 81: HTTP 404"),
                 ({("GET", "https://api.github.com/user"): 401}, (self.gh, 3, 81), finished, CLEAR + " 401"),
                 ({("GET", "https://gitlab.com/api/v4/user"): 403}, (self.gl, 3, 81), finished, "Could not clear the queued marker on acme/gitlab#3 comment 81: HTTP 403")]
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
        for name in ("reviewer_token", "run_desk", "add", "remove"):
            self.start(patch.object(acknowledge, name, side_effect=AssertionError(name + " ran in a test")))

    def status(self, project, number, comment):
        with sqlite3.connect(self.state / "requests.sqlite3") as db:
            return db.execute("SELECT status FROM requests WHERE project=? AND number=? AND comment=?",
                              (project, number, comment)).fetchone()[0]

    def test_failed_review_posts_one_fixed_comment_through_deskpost_and_nothing_else(self):
        ack = Acknowledgement(Jobs(self.state))
        bodies = []
        def desk(argv, *, cwd):
            bodies.append(Path(argv[-1]).read_text())
            self.assertEqual(cwd, self.gh.checkout)
        with patch.object(acknowledge, "run_desk", side_effect=desk) as post, patch.object(acknowledge, "remove"):
            ack.finished(self.gh, 3, 81, "failed")
            for status in ("reviewed", "partial", "empty", "over-budget", "model-unavailable"):
                ack.finished(self.gh, 3, 81, status)
            argv = post.call_args.args[0]
            self.assertEqual(argv, ["deskpost", "comment", "acme/github", "3", "--kind", "mr", "--body-file", argv[-1]])
            self.assertEqual(bodies, [NOTICE])
            self.assertNotIn("--head", argv)
            self.assertEqual(Path(argv[-1]).exists(), False)  # The body file was temporary.
            self.assertEqual(post.call_count, 1)
            post.side_effect = ReviewError("deskpost comment exited 5")
            with self.assertLogs("cursor_pr.acknowledge", level="WARNING") as logged:
                ack.finished(self.gl, 3, 81, "failed")
            self.assertEqual(post.call_args.args[0][:4], ["deskpost", "comment", "acme/gitlab", "3"])
            self.assertEqual(logged.output, ["WARNING:cursor_pr.acknowledge:Could not report the failed review on "
                                             "acme/gitlab#3: deskpost comment exited 5"])

    def test_cycle_marks_each_new_request_once_and_reports_only_a_failed_review(self):
        jobs = Jobs(self.state)
        since = {key: datetime(2026, 11, 15, tzinfo=timezone.utc) for key in self.projects}
        ack, threads, outcomes, public, results = Mock(), {p.slug: [] for p in self.projects.values()}, [], [True], []
        def desk(args, *, cwd, timeout, env=None):
            if args[0] == "deskmonitor":
                root = Path(env["PR_MONITOR_STATE_DIR"])
                root.mkdir(parents=True, exist_ok=True)
                (root / (args[2].replace("/", "__") + ".state")).write_text("3 abc ready OPEN CLEAN\n")
                return b""
            slug = args[3].split("#")[0]
            return json.dumps(dict(schema=1, kind="comments", partial=[],
                                   items=[dict(repo=slug, number=3, comments=threads[slug])])).encode()
        request = dict(authorId=7, databaseId=81, authorType="User", body="/review",
                       createdAt="2026-11-15T11:11:00Z", updatedAt="2026-11-15T11:11:00Z",
                       url=self.gh.change_url(3) + "#issuecomment-81")
        def review(project, number, state):
            outcomes.append((project.slug, number))
            return results.pop(0)
        with patch("cursor_pr.discovery.run_desk", side_effect=desk), \
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
            self.assertEqual([call.args for call in ack.queued.call_args_list],
                             [(self.gh, 3, 83), (self.gl, 3, 82)])
            self.assertEqual(outcomes, [("acme/github", 3), ("acme/gitlab", 3)])
            self.assertEqual([call.args for call in ack.finished.call_args_list],
                             [(self.gh, 3, 83, "reviewed"), (self.gl, 3, 82, "failed")])
            cycle(self.projects, self.state, jobs, since, review, ack)  # Already recorded: no second marker.
            self.assertEqual((ack.queued.call_count, ack.finished.call_count), (2, 2))

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
             patch("cursor_pr.poll.cycle"), patch("cursor_pr.poll.logging.basicConfig"), \
             patch("cursor_pr.acknowledge.Acknowledgement") as marker, self.assertLogs("cursor_pr.poll", level="WARNING"):
            main()
        marker.return_value.finished.assert_called_once_with(self.gh, 3, 81, "failed")
        reopened = Jobs(self.state)
        self.assertEqual((reopened.abandoned, reopened.pending(self.gh.key, 3)), ([], False))


if __name__ == "__main__":
    unittest.main()
