import contextlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import httpx

from cursor_pr import cli, engine, forge_status, inline, source
from cursor_pr.scanners import fence

HEAD = "a" * 40
SLUG = "org/project"
ADDRESS = SLUG + "#7"
ME = 42
SITES = {"github": "https://github.com/" + SLUG + "/pull/7",
         "gitlab": "https://gitlab.example/" + SLUG + "/-/merge_requests/7"}
REFS = {"base_sha": "b" * 40, "start_sha": "c" * 40, "head_sha": HEAD}
HOSTILE = "@all [click](https://evil.example) `x`"
FINDINGS = [{"path": "src/app.py", "line": 3, "end_line": 3, "title": "Unchecked input", "message": "Validate it."},
            {"path": "src/util.py", "line": 9, "end_line": 10, "title": "Off by one", "message": "Start at zero."}]
UNPLACEABLE = [{"path": "/etc/passwd", "line": 3, "end_line": 3, "title": "t", "message": "m"},
               {"path": "../escape.py", "line": 3, "end_line": 3, "title": "t", "message": "m"},
               {"path": "src/app.py", "line": 0, "end_line": 0, "title": "t", "message": "m"}]
USER = "https://gitlab.example/api/v4/user"
MR = "https://gitlab.example/api/v4/projects/org%2Fproject/merge_requests/7"
THREADS = MR + "/discussions"
REVIEW = "https://api.github.com/repos/org/project/pulls/7/reviews"
LOG = "cursor_pr.inline"


def outside_code_spans(text):
    """Drop the spans Markdown renders as code; only what remains is live Markdown."""
    return re.sub(r"(`+).*?\1", "", text, flags=re.S)


def summary(head=HEAD, author=ME, ident=900, kind="Bot", prefix=""):
    return {"authorLogin": "reviewer[bot]", "authorId": author, "createdAt": "2026-11-25T12:00:00Z",
            "body": prefix + f"Cursor review of {head}\nreviewed\n- ` src/app.py `:3 — ` Unchecked input `. ` Validate it. `",
            "databaseId": ident, "authorType": kind, "url": SITES["github"] + "#issuecomment-900",
            "updatedAt": "2026-11-25T12:00:00Z"}


def discussion(body, author=ME, kind="DiffNote"):
    return {"id": "d" + str(len(body)), "individual_note": False,
            "notes": [{"id": 1, "type": kind, "body": body, "author": {"id": author, "username": "reviewer"}}]}


def review(author, mark, commit=HEAD):
    return {"authorLogin": "x", "authorId": author, "state": "COMMENTED", "commitId": commit, "body": mark}


class InlineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.start(patch.dict(os.environ, {"GITLAB_API_BASE": "https://gitlab.example/api/v4",
                                           "REVIEW_STATE_DIR": str(self.root / "state")}))
        os.environ.pop("REVIEW_INLINE_COMMENTS", None)
        self.lookup = self.start(patch.object(forge_status, "reviewer_token", return_value="fixture"))
        # A scanner finding that must never reach an inline comment.
        scanner = {"tool": "shellcheck", "path": "demo.py", "state": "complete",
                   "findings": [{"path": "demo.py", "rule": "SC2086", "line": 1, "message": "Quote it."}]}
        self.start(patch.object(cli, "scan_files", return_value=[scanner]))
        self.requests, self.events = [], []
        self.head, self.comments, self.reviews, self.discussions = HEAD, [summary()], [], []
        self.mr, self.user = dict(diff_refs=REFS), {"id": ME, "username": "project_1_bot_x"}
        self.pages, self.refused, self.refuse_combined, self.status, self.error = {}, set(), False, 201, None
        client = httpx.Client
        transport = httpx.MockTransport(self.respond)
        self.start(patch.object(forge_status.httpx, "Client", side_effect=lambda **opts: client(transport=transport, **opts)))
        # Both names are bound at import: `inline.read` calls its own, `source.desk` the one in source.
        self.start(patch.object(inline, "run_desk", side_effect=self.desk))
        self.start(patch.object(source, "run_desk", side_effect=self.desk))
        # No desk tool may run for real, whatever a later change to the mocks.
        self.start(patch("cursor_pr.source.subprocess.run", side_effect=AssertionError("a real desk tool was executed")))

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def desk(self, argv, **kwargs):
        self.assertEqual(argv, ["deskread", argv[1], "--change", ADDRESS])
        kind = argv[1]
        self.events.append("desk:" + kind)
        payload = {"comments": self.comments, "reviews": self.reviews,
                   "change": {"url": SITES["github"], "state": "OPEN", "headSha": self.head}}[kind]
        return json.dumps({"schema": 1, "kind": kind, "items": [dict(repo=SLUG, number=7, **{kind: payload})], "partial": []}).encode()

    def respond(self, request):
        self.requests.append(request)
        self.events.append("http:" + request.method + " " + str(request.url))
        if self.error:
            raise self.error
        if request.url.host == "api.github.com":
            self.assertEqual(request.headers["Authorization"], "Bearer fixture")
            self.assertEqual((request.method, str(request.url)), ("POST", REVIEW))
            comments = json.loads(request.content)["comments"]
            refused = (self.refuse_combined and len(comments) > 1) or any(c["path"] in self.refused for c in comments)
        else:
            self.assertEqual(request.headers["PRIVATE-TOKEN"], "fixture")
            if str(request.url) == USER:
                return httpx.Response(200, json=self.user)
            self.assertIn("https://gitlab.example/api/v4/projects/org%2Fproject/merge_requests/7", str(request.url))
            if str(request.url) == MR:
                return httpx.Response(200, json=self.mr)
            self.assertTrue(str(request.url).startswith(THREADS))
            page = int(request.url.params["page"]) if request.method == "GET" else None
            if page is not None:
                self.assertEqual(dict(request.url.params), {"page": str(page), "per_page": "100"})
                return httpx.Response(200, json=self.pages.get(page, self.discussions),
                                      headers={"X-Next-Page": str(page + 1)} if page + 1 in self.pages else {})
            refused = json.loads(request.content)["position"]["new_path"] in self.refused
        return httpx.Response(422 if request.url.host == "api.github.com" and refused else 400 if refused else self.status, json={"id": 5})

    def exercise(self, provider, *options, findings=FINDINGS, model=None, desk_error=None, cache=None):
        change = source.Change(HEAD, [source.File("demo.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")], SITES[provider])
        args = cli.parser().parse_args(["--change", ADDRESS, "--repo", str(self.root), *options,
                                         "--cache-dir", str(cache or self.root / "cache"), "--max-input-tokens", "6000"])
        with patch.object(cli, "forge_change", return_value=change), \
             patch.object(cli, "run_desk", side_effect=desk_error or (lambda argv: self.events.append("deskpost"))) as desk, \
             patch.object(engine, "invoke", return_value={"findings": findings, "cursor_usage": None}), \
             (patch.object(cli, "review", side_effect=model) if model else contextlib.nullcontext()):
            report, code = cli.run_request(args)
        return report, code, desk.call_count

    def posted(self):
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]

    def test_github_posts_one_review_bound_to_the_reviewed_head(self):
        report, code, posts = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((code, report["inline_publication"], report["inline_posted"], report["inline_refused"],
                          report["publication"]), (0, "posted", 2, 0, "posted-or-already-present"))
        rows, _ = inline.comments(report)
        self.assertEqual(self.posted(), [{"commit_id": HEAD, "event": "COMMENT", "body": inline.marker(HEAD, rows), "comments": [
            dict(path="src/app.py", line=3, side="RIGHT", body="` Unchecked input `\n\n` Validate it. `"),
            dict(path="src/util.py", line=9, side="RIGHT", body="` Off by one `\n\n` Start at zero. `")]}])
        # The summary goes through deskpost first, and the head is the last thing read before the write.
        self.assertEqual(self.events, ["deskpost", "desk:comments", "desk:reviews", "desk:change", "http:POST " + REVIEW])
        self.assertEqual((posts, self.lookup.call_args.args), (1, ("github", SLUG, self.root)))
        self.assertNotIn("fixture", json.dumps(report))

    def test_gitlab_posts_one_discussion_per_finding_with_diff_refs(self):
        report, code, posts = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual((code, report["inline_publication"], report["inline_posted"], report["inline_refused"]), (0, "posted", 2, 0))
        self.assertEqual(self.posted(), [dict(body=f"{body}\n\n{inline.marker(HEAD, [path, line, body])}", commit_id=HEAD,
                                              position={"position_type": "text", **REFS, "new_path": path, "old_path": path, "new_line": line})
                                         for path, line, body in inline.comments(report)[0]])
        self.assertEqual(self.events, ["deskpost", "http:GET " + USER, "http:GET " + MR, "http:GET " + THREADS + "?page=1&per_page=100",
                                      "desk:change", *(["http:POST " + THREADS] * 2)])
        self.assertEqual((posts, self.lookup.call_args.args), (1, ("gitlab", SLUG, self.root)))

    def test_the_reviewer_identity_is_the_newest_bot_summary_on_github_and_the_token_on_gitlab(self):
        # A person quoting the summary line is not the reviewer; the newest bot summary names it.
        self.comments = [summary(ident=900), summary(ident=902, author=7, kind="User"), summary(ident=901, author=99)]
        self.assertEqual(self.exercise("github", "--post", "--inline-comments")[0]["inline_publication"], "posted")
        mark = self.posted()[0]["body"]
        self.reviews, self.requests = [review(ME, mark)], []
        self.assertEqual(self.exercise("github", "--post", "--inline-comments")[0]["inline_publication"], "posted")
        self.reviews, self.requests = [review(99, mark)], []
        self.assertEqual(self.exercise("github", "--post", "--inline-comments")[0]["inline_publication"], "already-present")
        # deskpost's audit lines may sit above or below the summary's first line.
        self.comments, self.reviews, self.requests = [summary(author=99, ident=903, prefix="<!-- deskpost audit -->\n")], [], []
        self.assertEqual(self.exercise("github", "--post", "--inline-comments")[0]["inline_publication"], "posted")
        # On GitLab the token's own user is the reviewer, whoever posted before it.
        self.user = {"id": 77, "username": "project_1_bot_x"}
        self.discussions = [discussion(f"` t `\n\n` m `\n\n{inline.marker(HEAD, row)}") for row in inline.comments({"findings": FINDINGS})[0]]
        report, *_ = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], report["inline_posted"]), ("posted", 2))

    def test_a_repeat_of_the_reviewer_identical_set_is_skipped_on_github(self):
        self.exercise("github", "--post", "--inline-comments")
        mark = self.posted()[0]["body"]
        for earlier in (review(7, mark), review(ME, mark, "b" * 40), review(ME, mark + "\nplanted")):
            self.requests.clear()
            self.reviews = [earlier]
            self.assertEqual(self.exercise("github", "--post", "--inline-comments")[0]["inline_publication"], "posted")
            self.assertEqual(len(self.posted()), 1)
        self.reviews, self.requests = [review(ME, mark)], []
        with self.assertNoLogs(LOG, level="WARNING"):
            report, code, posts = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((code, report["inline_publication"], report["publication"], posts), (0, "already-present", "posted-or-already-present", 1))
        self.assertEqual((self.requests, self.events[-1]), ([], "desk:reviews"))

    def test_a_repeat_of_the_reviewer_identical_findings_is_skipped_on_gitlab(self):
        rows, _ = inline.comments({"findings": FINDINGS})
        first, second = (inline.marker(HEAD, row) for row in rows)
        # Only our closing marker for this head counts, not one for another head.
        self.discussions = [discussion("see ` x `\n\n" + inline.marker("d" * 40, rows[1]) + "\n\n" + first)]
        report, *_ = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], report["inline_posted"]), ("posted", 1))
        self.assertEqual([sent["position"]["new_path"] for sent in self.posted()], ["src/util.py"])
        # Not ours either: a marker quoted by somebody else, one carried by something that is not a diff
        # discussion, one planted inside a finding's code span, or one that does not close the body.
        self.discussions += [discussion(second, author=7), discussion(second, kind=None), discussion(f"` t `\n\n` {second} `"),
                             discussion(second + "\n\nlater reply")]
        self.assertEqual(self.exercise("gitlab", "--post", "--inline-comments")[0]["inline_posted"], 1)
        self.discussions += [discussion(f"` t `\n\n` m `\n\n{second}")]
        self.requests.clear()
        with self.assertNoLogs(LOG, level="WARNING"):
            report, code, posts = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual((code, report["inline_publication"], posts), (0, "already-present", 1))
        self.assertEqual([r.method for r in self.requests], ["GET", "GET", "GET"])

    def test_discussion_pages_are_read_to_the_end_or_the_run_stops(self):
        self.pages = {1: [], 2: [discussion(f"` t `\n\n` m `\n\n{inline.marker(HEAD, row)}") for row in inline.comments({"findings": FINDINGS})[0]]}
        report, *_ = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], self.events[-2:]), ("already-present", [
            "http:GET " + THREADS + "?page=1&per_page=100", "http:GET " + THREADS + "?page=2&per_page=100"]))
        # An unread page could hold an earlier post of ours, so the run stops instead of risking a duplicate.
        self.pages = dict.fromkeys(range(1, 24), [])
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, *_ = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual(report["inline_publication"], "failed")
        self.assertIn("Too many discussions", logs.output[0])
        self.assertFalse(any(r.method == "POST" for r in self.requests))

    def test_a_moved_head_refuses_to_post(self):
        self.head = "b" * 40
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, code, posts = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((code, report["inline_publication"], report["publication"], posts), (0, "stale", "posted-or-already-present", 1))
        self.assertEqual((self.requests, logs.output), ([], [
            "WARNING:cursor_pr.inline:Inline comments for org/project#7 were not posted (the change moved on since the review)"]))
        # GitLab learns the head from the merge request's diff refs and, last, from the same deskread.
        for merge_request, head, reads in ((REFS | {"head_sha": "b" * 40}, HEAD, [USER, MR]),
                                           (REFS | {"start_sha": None}, HEAD, [USER, MR]),
                                           (REFS, "b" * 40, [USER, MR, THREADS + "?page=1&per_page=100"])):
            self.mr, self.head, self.requests = dict(diff_refs=merge_request), head, []
            with self.assertLogs(LOG, level="WARNING"):
                report, *_ = self.exercise("gitlab", "--post", "--inline-comments")
            self.assertEqual((report["inline_publication"], [str(r.url) for r in self.requests]), ("stale", reads))

    def test_untrusted_text_is_quoted_and_nothing_else_from_the_report_is_posted(self):
        findings = [{"path": "@all", "line": 3, "end_line": 3, "title": HOSTILE, "message": "[x](https://evil.example) ``` @team"},
                    {"path": "src/app.py", "line": 7, "end_line": 7, "title": "L" * 5000, "message": "M" * 9000}, *UNPLACEABLE]
        report, *_ = self.exercise("github", "--post", "--inline-comments", findings=findings)
        self.assertEqual((report["inline_publication"], report["inline_posted"], report["inline_refused"]), ("posted", 2, 3))
        [review_] = self.posted()
        self.assertEqual(review_["body"], inline.marker(HEAD, inline.comments(report)[0]))
        self.assertEqual([c["path"] for c in review_["comments"]], ["@all", "src/app.py"])
        self.assertEqual(review_["comments"][0]["body"],
                         f"{fence(HOSTILE, 255)}\n\n{fence('[x](https://evil.example) ``` @team', 2000)}")
        self.assertLessEqual(len(review_["comments"][1]["body"]), 255 + 2000 + 12)
        self.exercise("gitlab", "--post", "--inline-comments", findings=findings)
        threads = [sent["body"] for sent in self.posted() if "position" in sent]
        self.assertEqual(len(threads), 2)
        for text in [review_["body"], *(c["body"] for c in review_["comments"]), *threads]:
            for marker in ("@all", "@team", "evil.example", "``", "LLLL"):
                self.assertNotIn(marker, outside_code_spans(text))
            # No path text, scanner finding, report key or credential rides along.
            for marker in ("demo.py", "SC2086", "posted-or-already-present", "fixture", "pass", "escape"):
                self.assertNotIn(marker, text)

    def test_a_line_the_forge_refuses_is_skipped_and_counted(self):
        self.refused = {"src/util.py"}
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, code, posts = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual((code, report["inline_publication"], report["inline_posted"], report["inline_refused"], posts), (0, "posted", 1, 1, 1))
        self.assertEqual(len(self.posted()), 2)
        self.assertEqual(logs.output, ["WARNING:cursor_pr.inline:Inline comments for org/project#7: 1 posted, 1 refused by the forge or left out"])
        # GitHub takes a review whole: one refused line splits the review into one review per finding.
        self.refuse_combined, self.requests = True, []
        with self.assertLogs(LOG, level="WARNING"):
            report, *_ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], report["inline_posted"], report["inline_refused"]), ("posted", 1, 1))
        sent = self.posted()
        self.assertEqual([len(r["comments"]) for r in sent], [2, 1, 1])
        self.assertEqual(sent[0]["body"], sent[1]["body"])
        self.refused, self.refuse_combined = set(), False
        self.assertEqual(self.exercise("github", "--post", "--inline-comments")[0]["inline_publication"], "posted")
        self.assertEqual([len(r["comments"]) for r in self.posted()], [2, 1, 1, 2])

    def test_it_runs_only_when_enabled_for_a_posted_forge_review(self):
        report, code, _ = self.exercise("github", "--post")
        self.assertEqual((code, report["inline_publication"]), (0, "skipped"))
        with patch.dict(os.environ, {"REVIEW_INLINE_COMMENTS": "true"}):
            self.assertEqual(self.exercise("github", "--post")[0]["inline_publication"], "skipped")
        for args in (["--change", ADDRESS, "--inline-comments"], ["--change", ADDRESS, "--estimate", "--inline-comments"],
                     ["--base", "x", "--inline-comments"]):
            parsed = cli.parser().parse_args(args)
            self.assertEqual(inline.outcome(parsed, {"publication": "posted-or-already-present"}), {"inline_publication": "skipped"})
        self.assertEqual(self.requests, [])
        self.assertFalse([e for e in self.events if e != "deskpost"])
        with patch.dict(os.environ, {"REVIEW_INLINE_COMMENTS": "1"}):
            self.assertEqual(self.exercise("github", "--post")[0]["inline_publication"], "posted")
            self.assertEqual(self.exercise("github", "--estimate")[0]["inline_publication"], "skipped")
        # A separate cache, or the earlier answer with two findings would be reused.
        report, code, _ = self.exercise("github", "--post", "--inline-comments", findings=[], cache=self.root / "empty-cache")
        self.assertEqual((code, report["inline_publication"], report["findings"]), (0, "skipped", []))
        report, code, posts = self.exercise("github", "--post", "--inline-comments", desk_error=source.ReviewError("deskpost comment exited 1"))
        self.assertEqual((code, report["publication"], posts), (2, "failed", 1))
        self.assertNotIn("inline_publication", report)

    def test_failures_never_change_the_exit_code_or_the_summary(self):
        self.status = 403
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, code, posts = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((code, report["inline_publication"], report["publication"], posts), (0, "failed", "posted-or-already-present", 1))
        self.assertEqual(logs.output, ["WARNING:cursor_pr.inline:Inline comments for org/project#7 were not posted (HTTP 403)"])
        self.status = 201
        # A failed model leaves no finding to place, with or without the flag.
        for options in (("--post",), ("--post", "--inline-comments")):
            with self.assertNoLogs(LOG, level="WARNING"):
                report, code, posts = self.exercise("github", *options, model=engine.CursorFailure("Cursor CLI is not installed", 0))
            self.assertEqual((code, report["inline_publication"], report["publication"], posts),
                             (2, "skipped", "posted-or-already-present", 1))
        # Without a reviewer identity nothing is posted, rather than a duplicate.
        for provider, key, value in (("github", "comments", []), ("github", "comments", [summary(ident="900")]),
                                     ("github", "comments", [summary(kind="User")]), ("gitlab", "user", {"message": "401"}),
                                     ("gitlab", "user", {"id": True})):
            self.comments, self.user, self.requests = [summary()], {"id": ME}, []
            setattr(self, key, value)
            with self.assertLogs(LOG, level="WARNING") as logs:
                report, code, posts = self.exercise(provider, "--post", "--inline-comments")
            self.assertEqual((code, report["inline_publication"], posts), (0, "failed", 1))
            self.assertEqual([r.getMessage() for r in logs.records],
                             ["Inline comments for org/project#7 were not posted (Could not identify the reviewer identity on the change)"])
            self.assertFalse(any(r.method == "POST" for r in self.requests))
        self.comments, self.user = [summary()], {"id": ME}
        self.error = httpx.ConnectError("https://gitlab.example/api/v4/private?token=fixture")
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, *_ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], logs.output), ("failed", [
            "WARNING:cursor_pr.inline:Inline comments for org/project#7 were not posted (ConnectError)"]))
        # A permission failure on a discussion is not a refused line: nothing is counted as placed.
        self.error, self.status, self.requests = None, 403, []
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, *_ = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual(({k: v for k, v in report.items() if k.startswith("inline")}, logs.output), ({"inline_publication": "failed"}, [
            "WARNING:cursor_pr.inline:Inline comments for org/project#7 were not posted (HTTP 403)"]))
        self.assertEqual([r.method for r in self.requests], ["GET", "GET", "GET", "POST"])
        self.status, self.requests = 302, []
        with self.assertLogs(LOG, level="WARNING"):
            report, *_ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], len(self.requests)), ("failed", 1))

    def test_the_poller_child_inherits_the_environment_variable(self):
        from cursor_pr.config import project_from
        from cursor_pr.poll import run_review
        project = project_from(dict(provider="github", url="https://github.com/" + SLUG, requester_ids=[7], checkout=str(self.root)))
        with patch.dict(os.environ, {"REVIEW_INLINE_COMMENTS": "1"}), patch("cursor_pr.poll.subprocess.run") as run:
            run.return_value.stdout = b'{"state": "reviewed", "publication": "posted-or-already-present"}'
            self.assertEqual(run_review(project, 7, self.root), "reviewed")
        self.assertEqual(run.call_args.kwargs["env"]["REVIEW_INLINE_COMMENTS"], "1")
        self.assertNotIn("--inline-comments", run.call_args.args[0])
