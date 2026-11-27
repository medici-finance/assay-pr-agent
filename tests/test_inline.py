import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import httpx

from cursor_pr import cli, engine, forge_status, inline, source
from cursor_pr.config import project_from
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
NOTES = "https://api.github.com/repos/org/project/pulls/7/comments"
LISTED = NOTES + "?page=1&per_page=100"
SUMMARIES = "https://api.github.com/repos/org/project/issues/7/comments?page=1&per_page=100"
LOG = "cursor_pr.inline"


def outside_code_spans(text):
    """Drop the spans Markdown renders as code; only what remains is live Markdown."""
    return re.sub(r"(`+).*?\1", "", text, flags=re.S)


def summary(head=HEAD, author=ME, kind="Bot", app=85):
    return {"user": {"id": author, "type": kind}, "performed_via_github_app": {"id": app},
            "body": f"Cursor review of {head}\nreviewed\n- Input finding"}


def discussion(body, author=ME, kind="DiffNote"):
    return {"id": "d" + str(len(body)), "individual_note": False,
            "notes": [{"id": 1, "type": kind, "body": body, "author": {"id": author, "username": "reviewer"}}]}


def note(author, body):
    """A pull request review comment as GitHub lists it."""
    return {"user": {"login": "x", "id": author, "type": "Bot"}, "commit_id": HEAD, "body": body}


class InlineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.start(patch.dict(os.environ, {"GITLAB_API_BASE": "https://gitlab.example/api/v4", "REVIEW_FORGE_APP_ID": "85",
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
        self.review_pages, self.fail_after = {}, None
        self.pages, self.refused, self.refuse_combined, self.status, self.error = {}, set(), False, 201, None
        client = httpx.Client
        transport = httpx.MockTransport(self.respond)
        self.start(patch.object(forge_status.httpx, "Client", side_effect=lambda **opts: client(transport=transport, **opts)))
        self.start(patch.object(inline, "run_forge", side_effect=self.forge))
        self.policy = self.start(patch.object(inline, "check_publication", side_effect=self.check_policy))
        # No forge helper may run for real, whatever a later change to the mocks.
        self.start(patch("cursor_pr.source.subprocess.run", side_effect=AssertionError("a real forge helper was executed")))

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def forge(self, verb, project, *, numbers=(), **kwargs):
        self.assertEqual((project, list(numbers)), (self.project, [7]))
        self.events.append("forge:" + verb)
        self.assertEqual(verb, "change")
        return {"url": SITES["github"], "state": "open", "headSha": self.head}

    def check_policy(self, project, number, head, body):
        self.assertEqual((project, number, head), (self.project, 7, HEAD))
        self.events.append("policy")
        directory = self.root / "state" / "inline-publications"
        key = hashlib.sha256(json.dumps([project.key, number, head]).encode()).hexdigest()
        with (directory / (key + ".lock")).open("a") as competing, self.assertRaises(BlockingIOError):
            fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def respond(self, request):
        self.requests.append(request)
        self.events.append("http:" + request.method + " " + str(request.url))
        if self.error:
            raise self.error
        if request.url.host == "api.github.com":
            self.assertEqual(request.headers["Authorization"], "Bearer fixture")
            if request.method == "GET":
                if str(request.url) == SUMMARIES:
                    return httpx.Response(200, json=self.comments)
                page = int(request.url.params["page"])
                self.assertEqual(str(request.url), NOTES + f"?page={page}&per_page=100")
                return httpx.Response(200, json=self.review_pages.get(page, self.reviews if page == 1 else []))
            self.assertEqual((request.method, str(request.url)), ("POST", REVIEW))
            comments = json.loads(request.content)["comments"]
            refused = (self.refuse_combined and len(comments) > 1) or any(c["path"] in self.refused for c in comments)
            if refused and getattr(self, "push_on_refusal", False):
                self.head = "b" * 40  # A contributor pushes just as the batch is refused.
            if self.fail_after is not None and len(comments) == 1 and not refused:
                if self.fail_after == 0:
                    return httpx.Response(502)
                self.fail_after -= 1
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
                more = ({"Link": f'<{THREADS}?page={page + 1}&per_page=100>; rel="next"'} if getattr(self, "link_only", False)
                        else {"X-Next-Page": str(page + 1)})
                return httpx.Response(200, json=self.pages.get(page, self.discussions),
                                      headers=more if page + 1 in self.pages else {})
            refused = json.loads(request.content)["position"]["new_path"] in self.refused
            if self.fail_after is not None and not refused:
                if self.fail_after == 0:
                    return httpx.Response(502)
                self.fail_after -= 1
        return httpx.Response(422 if request.url.host == "api.github.com" and refused else 400 if refused else self.status, json={"id": 5})

    def exercise(self, provider, *options, findings=FINDINGS, model=None, desk_error=None, cache=None, previous=None):
        files = [source.File("demo.py", "@@ -0,0 +1 @@\n+x = 1\n", "added")]
        if previous:
            files.append(source.File("src/app.py", "", "renamed", previous=previous))
        change = source.Change(HEAD, files, SITES[provider])
        args = cli.parser().parse_args(["--change", ADDRESS, "--repo", str(self.root), *options,
                                         "--cache-dir", str(cache or self.root / "cache"), "--max-input-tokens", "6000"])
        site = SITES[provider].removesuffix("/pull/7" if provider == "github" else "/-/merge_requests/7")
        self.project = project_from(dict(provider=provider, url=site, checkout=str(self.root), requester_ids=[7]))
        args.forge_project = self.project
        with patch.object(cli, "forge_change", return_value=change), \
             patch.object(cli, "post_comment", side_effect=desk_error or (lambda *call: self.events.append("summary"))) as desk, \
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
            dict(path=path, line=line, side="RIGHT", body=f"{body}\n\n{inline.marker(HEAD, [path, line, body])}")
            for path, line, body in rows]}])
        self.assertEqual([c["body"].split("\n\n")[:2] for c in self.posted()[0]["comments"]],
                         [["` Unchecked input `", "` Validate it. `"], ["` Off by one `", "` Start at zero. `"]])
        # The summary is posted first, and the head is the last thing read before the write.
        self.assertEqual(self.events, ["summary", "http:GET " + SUMMARIES, "http:GET " + LISTED, "forge:change", "policy", "http:POST " + REVIEW])
        self.assertEqual((posts, self.lookup.call_args.args), (1, ("github", SLUG, self.root)))
        self.assertNotIn("fixture", json.dumps(report))

    def test_gitlab_posts_one_discussion_per_finding_with_diff_refs(self):
        report, code, posts = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual((code, report["inline_publication"], report["inline_posted"], report["inline_refused"]), (0, "posted", 2, 0))
        self.assertEqual(self.posted(), [dict(body=f"{body}\n\n{inline.marker(HEAD, [path, line, body])}", commit_id=HEAD,
                                              position={"position_type": "text", **REFS, "new_path": path, "old_path": path, "new_line": line})
                                         for path, line, body in inline.comments(report)[0]])
        self.assertEqual(self.events, ["summary", "http:GET " + USER, "http:GET " + MR, "http:GET " + THREADS + "?page=1&per_page=100",
                                      "forge:change", "policy", *(["http:POST " + THREADS] * 2)])
        self.assertEqual((posts, self.lookup.call_args.args), (1, ("gitlab", SLUG, self.root)))

    def test_github_binds_review_identity_to_configured_app(self):
        self.comments = [summary(author=99, app=99), summary(), summary(author=7, kind="User")]
        self.assertEqual(self.exercise("github", "--post", "--inline-comments")[0]["inline_publication"], "posted")
        bodies = [c["body"] for c in self.posted()[0]["comments"]]
        self.reviews, self.requests = [note(99, body) for body in bodies], []
        self.assertEqual(self.exercise("github", "--post", "--inline-comments")[0]["inline_publication"], "posted")
        self.reviews, self.requests = [note(ME, body) for body in bodies], []
        self.assertEqual(self.exercise("github", "--post", "--inline-comments")[0]["inline_publication"], "already-present")

    def test_gitlab_rename_uses_the_old_path_from_the_reviewed_diff(self):
        report, *_ = self.exercise("gitlab", "--post", "--inline-comments", previous="src/old.py")
        self.assertEqual(report["inline_posted"], 2)
        self.assertEqual([item["position"]["old_path"] for item in self.posted()], ["src/old.py", "src/util.py"])
        self.assertIn("src/old.py", self.policy.call_args.args[-1])

    def test_rendered_content_is_checked_before_any_inline_post(self):
        self.policy.side_effect = source.ReviewError("publication policy refused the rendered comment")
        with self.assertLogs(LOG, level="WARNING"):
            report, code, _ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], code, self.posted()), ("failed", 0, []))
        self.assertIn("Validate it.", self.policy.call_args.args[-1])
        self.assertIn("<!-- assay-pr-agent:inline:", self.policy.call_args.args[-1])

    def test_a_repeat_of_the_reviewer_identical_findings_is_skipped_on_github(self):
        rows, _ = inline.comments({"findings": FINDINGS})
        first, second = (f"{row[2]}\n\n{inline.marker(HEAD, row)}" for row in rows)
        # Not ours: another author's comment, a marker for another head, and one that does not close the body.
        for earlier in (note(7, first), note(ME, f"{rows[0][2]}\n\n{inline.marker('d' * 40, rows[0])}"), note(ME, first + "\nplanted")):
            self.requests.clear()
            self.reviews = [earlier]
            report, *_ = self.exercise("github", "--post", "--inline-comments")
            self.assertEqual((report["inline_publication"], report["inline_posted"]), ("posted", 2))
            self.assertEqual([len(r["comments"]) for r in self.posted()], [2])
        # A later run with a different set of findings posts only the one that is not there yet.
        self.reviews, self.requests = [note(ME, first)], []
        report, *_ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], report["inline_posted"]), ("posted", 1))
        [sent] = self.posted()
        self.assertEqual(([c["path"] for c in sent["comments"]], sent["body"]), (["src/util.py"], inline.marker(HEAD, rows[1:])))
        self.reviews, self.requests = [note(ME, first), note(ME, second)], []
        with self.assertNoLogs(LOG, level="WARNING"):
            report, code, posts = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((code, report["inline_publication"], report["publication"], posts), (0, "already-present", "posted-or-already-present", 1))
        self.assertEqual(([r.method for r in self.requests], self.events[-1]), (["GET", "GET"], "http:GET " + LISTED))

    def test_a_split_review_that_stops_partway_is_finished_by_the_next_run(self):
        # The combined review is refused, the first single one lands, and the next fails in transit.
        self.refuse_combined, self.fail_after = True, 1
        with self.assertLogs(LOG, level="WARNING"):
            report, *_ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], report["inline_posted"]), ("failed", 1))
        landed = self.posted()[1]["comments"]
        self.assertEqual([c["path"] for c in landed], ["src/app.py"])
        self.refuse_combined, self.fail_after, self.requests = False, None, []
        self.reviews = [note(ME, c["body"]) for c in landed]
        report, *_ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], report["inline_posted"]), ("posted", 1))
        self.assertEqual([c["path"] for r in self.posted() for c in r["comments"]], ["src/util.py"])

    def test_a_gitlab_transport_failure_keeps_earlier_successes_and_refusals(self):
        self.fail_after = 1
        self.refused = {"src/util.py"}
        last = dict(FINDINGS[0], line=4, end_line=4)
        with self.assertLogs(LOG, level="WARNING"):
            report, *_ = self.exercise("gitlab", "--post", "--inline-comments", findings=FINDINGS + [last])
        self.assertEqual((report["inline_publication"], report["inline_posted"], report["inline_refused"]),
                         ("failed", 1, 1))

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
        # A next page named only in the Link header is read as well.
        self.link_only, self.requests = True, []
        report, *_ = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], [r.method for r in self.requests].count("POST")), ("already-present", 0))
        self.link_only = False
        # An unread page could hold an earlier post of ours, so the run stops instead of risking a duplicate.
        self.pages = dict.fromkeys(range(1, 24), [])
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, *_ = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual(report["inline_publication"], "failed")
        self.assertIn("Too many discussions", logs.output[0])
        self.assertFalse(any(r.method == "POST" for r in self.requests))

    def test_review_pages_are_read_to_the_end_or_the_run_stops(self):
        self.exercise("github", "--post", "--inline-comments")
        ours = [note(ME, c["body"]) for c in self.posted()[0]["comments"]]
        # A full page means there may be another: the earlier comments of ours are on the second.
        self.review_pages, self.requests = {1: [note(7, "other")] * 100, 2: ours}, []
        report, *_ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], [str(r.url) for r in self.requests]),
                         ("already-present", [SUMMARIES, NOTES + "?page=1&per_page=100", NOTES + "?page=2&per_page=100"]))
        self.review_pages, self.requests = dict.fromkeys(range(1, 24), [note(7, "other")] * 100), []
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, *_ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual(report["inline_publication"], "failed")
        self.assertIn("Too many GitHub comments", logs.output[0])
        self.assertFalse(any(r.method == "POST" for r in self.requests))

    def test_a_moved_head_refuses_to_post(self):
        self.head = "b" * 40
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, code, posts = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((code, report["inline_publication"], report["publication"], posts), (0, "stale", "posted-or-already-present", 1))
        self.assertEqual(([str(r.url) for r in self.requests], logs.output), ([SUMMARIES, LISTED], [
            "WARNING:cursor_pr.inline:Inline comments for org/project#7 were not posted (the change moved on since the review)"]))
        # GitLab learns the head from the merge request's diff refs and, last, from the same forge read.
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
        self.assertEqual(review_["comments"][0]["body"].rsplit("\n\n", 1)[0],
                         f"{fence(HOSTILE, 255)}\n\n{fence('[x](https://evil.example) ``` @team', 2000)}")
        self.assertLessEqual(len(review_["comments"][1]["body"].rsplit("\n\n", 1)[0]), 255 + 2000 + 12)
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
        self.assertNotEqual(sent[0]["body"], sent[1]["body"])
        self.refused, self.refuse_combined = set(), False
        self.assertEqual(self.exercise("github", "--post", "--inline-comments")[0]["inline_publication"], "posted")
        self.assertEqual([len(r["comments"]) for r in self.posted()], [2, 1, 1, 2])

    def test_a_push_between_a_refused_review_and_its_retries_stops_them(self):
        self.refuse_combined = self.push_on_refusal = True
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, *_ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], report["inline_posted"]), ("stale", 0))
        self.assertEqual([len(r["comments"]) for r in self.posted()], [2])  # No review of one finding followed.
        self.assertIn("the change moved on since the review", logs.output[0])

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
        self.assertFalse([e for e in self.events if e != "summary"])
        with patch.dict(os.environ, {"REVIEW_INLINE_COMMENTS": "1"}):
            self.assertEqual(self.exercise("github", "--post")[0]["inline_publication"], "posted")
            self.assertEqual(self.exercise("github", "--estimate")[0]["inline_publication"], "skipped")
        # A separate cache, or the earlier answer with two findings would be reused.
        report, code, _ = self.exercise("github", "--post", "--inline-comments", findings=[], cache=self.root / "empty-cache")
        self.assertEqual((code, report["inline_publication"], report["findings"]), (0, "skipped", []))
        report, code, posts = self.exercise("github", "--post", "--inline-comments", desk_error=source.ReviewError("pr-agent-forge comment exited 1"))
        self.assertEqual((code, report["publication"], posts), (2, "failed", 1))
        self.assertNotIn("inline_publication", report)

    def test_failures_never_change_the_exit_code_or_the_summary(self):
        self.status = 403
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, code, posts = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((code, report["inline_publication"], report["publication"], posts), (0, "failed", "posted-or-already-present", 1))
        self.assertEqual(logs.output, ["WARNING:cursor_pr.inline:Inline comment publication for org/project#7 stopped (HTTP 403)"])
        self.status = 201
        # A failed model leaves no finding to place, with or without the flag.
        for options in (("--post",), ("--post", "--inline-comments")):
            with self.assertNoLogs(LOG, level="WARNING"):
                report, code, posts = self.exercise("github", *options, model=engine.CursorFailure("Cursor CLI is not installed", 0))
            self.assertEqual((code, report["inline_publication"], report["publication"], posts),
                             (2, "skipped", "posted-or-already-present", 1))
        # Without a reviewer identity nothing is posted, rather than a duplicate.
        for provider, key, value in (("github", "comments", []), ("github", "comments", [summary(author="42")]),
                                     ("github", "comments", [summary(kind="User")]), ("gitlab", "user", {"message": "401"}),
                                     ("gitlab", "user", {"id": True})):
            self.comments, self.user, self.requests = [summary()], {"id": ME}, []
            setattr(self, key, value)
            with self.assertLogs(LOG, level="WARNING") as logs:
                report, code, posts = self.exercise(provider, "--post", "--inline-comments")
            self.assertEqual((code, report["inline_publication"], posts), (0, "failed", 1))
            self.assertEqual([r.getMessage() for r in logs.records],
                             ["Inline comment publication for org/project#7 stopped (Could not identify the reviewer identity on the change)"])
            self.assertFalse(any(r.method == "POST" for r in self.requests))
        self.comments, self.user = [summary()], {"id": ME}
        self.error = httpx.ConnectError("https://gitlab.example/api/v4/private?token=fixture")
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, *_ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], logs.output), ("failed", [
            "WARNING:cursor_pr.inline:Inline comment publication for org/project#7 stopped (ConnectError)"]))
        # A permission failure on a discussion is not a refused line: nothing is counted as placed.
        self.error, self.status, self.requests = None, 403, []
        with self.assertLogs(LOG, level="WARNING") as logs:
            report, *_ = self.exercise("gitlab", "--post", "--inline-comments")
        self.assertEqual(({k: v for k, v in report.items() if k.startswith("inline")}, logs.output), ({"inline_publication": "failed", "inline_posted": 0, "inline_refused": 0}, [
            "WARNING:cursor_pr.inline:Inline comment publication for org/project#7 stopped (HTTP 403)"]))
        self.assertEqual([r.method for r in self.requests], ["GET", "GET", "GET", "POST"])
        self.status, self.requests = 302, []
        with self.assertLogs(LOG, level="WARNING"):
            report, *_ = self.exercise("github", "--post", "--inline-comments")
        self.assertEqual((report["inline_publication"], [r.method for r in self.requests]), ("failed", ["GET", "GET", "POST"]))

    def test_the_poller_child_inherits_the_environment_variable(self):
        from cursor_pr.config import project_from
        from cursor_pr.poll import run_review
        project = project_from(dict(provider="github", url="https://github.com/" + SLUG, requester_ids=[7], checkout=str(self.root)))
        with patch.dict(os.environ, {"REVIEW_INLINE_COMMENTS": "1"}), patch("cursor_pr.poll.supervised") as run:
            run.return_value = b'{"state": "reviewed", "publication": "posted-or-already-present"}'
            self.assertEqual(run_review(project, 7, self.root), "reviewed")
        self.assertEqual(run.call_args.kwargs["env"]["REVIEW_INLINE_COMMENTS"], "1")
        self.assertNotIn("--inline-comments", run.call_args.args[0])
