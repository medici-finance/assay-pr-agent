import contextlib
from datetime import datetime, timezone
import io
import json
import os
import re
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cursor_pr import acknowledge, cli, context, discovery, source
from cursor_pr.config import project_from
from cursor_pr.poll import Jobs, cycle, run_review

ATTRIBUTION = "Review by [Assay PR Agent](https://pr-agent.assay.guide/)."
HEAD = "a" * 40


class HelpTextTests(unittest.TestCase):
    def test_every_command_has_a_help_line(self):
        # A command added to the table without a description fails here, before it can fail a request.
        self.assertEqual(set(discovery.HELP), set(discovery.COMMANDS.values()))
        rows = [line for line in discovery.help_text().splitlines() if line.startswith("- ")]
        self.assertEqual([re.match(r"- `(/[\w-]+)`: \S", row).group(1) for row in rows], list(discovery.COMMANDS))
        with patch.dict(discovery.COMMANDS, {"/review-huge": "huge"}), self.assertRaises(KeyError):
            discovery.help_text()

    def test_the_text_is_fixed_and_closes_like_a_review(self):
        text = discovery.help_text()
        self.assertEqual((text.splitlines()[0], text.splitlines()[-1]), (discovery.HELP_HEADING, ATTRIBUTION))
        self.assertEqual(sorted(set(re.findall(r"`(/[\w-]+)`", text))), sorted(discovery.COMMANDS))
        self.assertLess(len(text.encode()), cli.FORGE_COMMENT_BYTES)


class HelpRequestTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.settings = dict(provider="github", url="https://github.com/acme/app", requester_ids=[7], checkout=tmp.name)
        self.gh = project_from(self.settings)
        self.since = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def note(self, comment, body, author=7):
        return dict(authorId=author, databaseId=comment, authorType="User", body=body,
                    createdAt="2026-07-01T00:00:00+00:00", updatedAt="2026-07-01T00:00:00+00:00",
                    url=f"{self.gh.change_url(3)}#issuecomment-{comment}")

    def test_the_command_is_a_request_only_as_the_whole_comment_of_a_requester(self):
        bodies = ["/help", " /help\n", "/help me", "/helpme", "> /help", "/Help", "/review /help"]
        self.assertEqual([discovery.is_request(self.gh, 3, self.note(81 + n, body), self.since) for n, body in enumerate(bodies)],
                         [True, True, False, False, False, False, False])
        self.assertFalse(discovery.is_request(self.gh, 3, self.note(90, "/help", author=8), self.since))

    def test_a_help_request_is_queued_and_run_as_itself_beside_a_review(self):
        notes = [self.note(81, "/review"), self.note(82, "/help"), self.note(83, "/help me")]
        jobs, ran = Jobs(self.state), []
        with patch.object(discovery, "open_changes", return_value=([3], True)), \
             patch.object(discovery, "comment_threads", return_value=({3: notes}, True)):
            cycle({self.gh.key: self.gh}, self.state, jobs, {self.gh.key: self.since},
                  lambda project, number, state, *kind: ran.append(kind) or "reviewed")
        self.assertEqual(ran, [(), ("help",)])
        self.assertEqual(Jobs(self.state).tier((self.gh.key, 3, 82)), "help")  # Survives a restart.

    def child_argv(self, *request):
        # What the poller starts for a /help, with every setting a review would pass on present.
        done = json.dumps({"state": "reviewed", "publication": "posted-or-already-present"}).encode()
        project = project_from(dict(self.settings, scanner_ci={"hadolint": {"producer": "lint.yml"}}))
        settings = {"REVIEW_MAX_INPUT_TOKENS": "4000", "REVIEW_VISUAL_MAX_INPUT_TOKENS": "9000", "REVIEW_MAX_FINDINGS": "2",
                    "REVIEW_MODEL": "some-model"}
        with patch.dict(os.environ, settings), patch("cursor_pr.poll.supervised", return_value=done) as run, \
                patch("cursor_pr.poll.history.interval", return_value=60):
            self.assertEqual(run_review(project, 3, self.state, "help", *request), "reviewed")
        return run.call_args.args[0]

    def test_the_child_gets_the_help_flag_and_nothing_of_a_review(self):
        argv = self.child_argv()
        self.assertIn("--help-comment", argv)
        for option in ("--visual", "--tier", "--max-input-tokens", "--max-findings", "--model", "--qualgen-store",
                       "--scanner-ci"):
            self.assertNotIn(option, argv)

    def answer(self, argv, forge, **environment):
        """Run the review child as the poller started it; return its exit code, its report and the forge calls."""
        calls = []

        def run_forge(verb, project, *, numbers=(), publication=None, timeout=45):
            calls.append((verb, publication))
            return forge if verb == "change" else {}

        # No provider key, login or budget is in the environment, and nothing of a review may be reached.
        env = {"PATH": os.environ["PATH"], "HOME": str(self.state), "CURSOR_REVIEW_POLLER_CHILD": "1",
               "REVIEW_STATE_DIR": str(self.state), "REVIEW_FORGE_PROJECT": json.dumps(self.settings), **environment}
        with contextlib.ExitStack() as stack:
            untouched = [stack.enter_context(patch.object(cli, name)) for name in (
                "review", "forge_change", "scan_files", "max_findings", "max_input_tokens", "scan_checkout")]
            untouched += [stack.enter_context(patch.object(cli.providers, "cursor_account")),
                          stack.enter_context(patch.object(cli.scanners, "ci_results")),
                          stack.enter_context(patch.object(cli.history, "screen")),
                          stack.enter_context(patch.object(cli.sarif, "write")),
                          stack.enter_context(patch.object(cli.forge_status, "post"))]
            stack.enter_context(patch.object(source, "run_forge", side_effect=run_forge))
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            stack.enter_context(patch("sys.argv", ["cursor-review", *argv[argv.index("cursor_pr.cli") + 1:]]))
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            err = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = cli.main()
            for mock in untouched:
                mock.assert_not_called()
        return code, json.loads(out.getvalue()) if out.getvalue() else err.getvalue(), calls

    def row(self):
        with contextlib.closing(sqlite3.connect(self.state / "requests.sqlite3")) as db:
            return db.execute("SELECT status, head FROM requests WHERE comment=82").fetchone()

    def test_help_is_published_once_on_the_current_head_without_a_model_a_scanner_or_a_check(self):
        jobs = Jobs(self.state)
        jobs.enqueue(self.gh.key, 3, 82)
        jobs.set_tier(self.gh.key, 3, 82, "help")
        self.assertEqual(jobs.next(), (self.gh.key, 3, 82))
        argv = self.child_argv(82)
        forge = dict(url=self.gh.change_url(3), state="open", headSha=HEAD, title="@all [hostile](https://evil.example)")
        # The check is asked for and still not posted: the answer is no review.
        code, report, calls = self.answer(argv, forge, REVIEW_FORGE_STATUS="1")
        self.assertEqual([verb for verb, _ in calls], ["change", "comment"])
        published = calls[1][1]
        self.assertEqual((published["Number"], published["Head"], published["Body"], published["AlreadyPosted"]),
                         (3, HEAD, discovery.help_text(), False))
        self.assertEqual((code, report["state"], report["publication"], report["forge_status"], report["model_invocations"]),
                         (0, "reviewed", "posted-or-already-present", "skipped", 0))
        self.assertNotIn("hostile", json.dumps(report))
        self.assertEqual(self.row(), ("reviewed", HEAD))
        # A second /help at the same head goes through the helper as already posted.
        code, report, calls = self.answer(argv[:argv.index("--queue")], forge)
        self.assertEqual((code, calls[1][1]["Body"], calls[1][1]["AlreadyPosted"]), (0, discovery.help_text(), True))
        # Run by hand, outside the poller, with a login profile configured: still no provider account
        # is selected and no login is waited for. Each is refused by its own check in main().
        with patch.object(cli, "auth_mode", return_value="login"), \
                patch.object(cli, "locked", side_effect=AssertionError("a login was waited for")), \
                patch.object(cli, "apply_saved_setup"), patch.object(cli, "forge_credentials"), \
                patch.object(cli, "load_config", return_value={self.gh.key: self.gh}):
            code, report, calls = self.answer(argv[:argv.index("--queue")], forge, CURSOR_REVIEW_POLLER_CHILD="0",
                                              REVIEW_LOGIN_PROFILE="work")
        self.assertEqual((code, [verb for verb, _ in calls]), (0, ["change", "comment"]))

    def test_help_on_a_closed_change_fails_and_posts_nothing(self):
        argv = self.child_argv()
        code, error, calls = self.answer(argv, dict(url=self.gh.change_url(3), state="closed", headSha=HEAD))
        self.assertEqual((code, [verb for verb, _ in calls]), (2, ["change"]))
        self.assertIn("different or closed change", error)

    def test_help_takes_no_review_option(self):
        posted = ["--change", "acme/app#3", "--post"]
        # Each case is refused for the one thing named: the others would be accepted without it.
        cases = ([*posted, "--visual"], [*posted, "--tier", "mid"], [*posted, "--max-findings", "3"],
                 [*posted, "--sarif", "out.sarif"], [*posted, "--path", "app.py"], [*posted, "--max-input-tokens", "100"],
                 [*posted, "--qualgen-store", "store"], [*posted, "--go-analysis-producer", "lint.yml"], [*posted, "--forge-status"],
                 ["--change", "acme/app#3"], ["--change", "acme/app#3", "--estimate"], ["--base", "HEAD", "--post"], ["--ci", "--post"])
        for options in cases:
            with self.subTest(options=options), patch.object(source, "run_forge") as forge:
                args = cli.parser().parse_args([*options, "--help-comment"])
                with self.assertRaisesRegex(source.ReviewError, "takes --change with --post and no review option"):
                    cli.execute(args)
                forge.assert_not_called()
        # With --change and --post alone the options pass, and it goes on to look for the project.
        with self.assertRaisesRegex(source.ReviewError, "configured review projects"):
            cli.execute(cli.parser().parse_args([*posted, "--help-comment"]))

    def test_a_failed_help_request_is_told_to_post_its_own_command_again(self):
        jobs = Jobs(self.state)
        posted = []
        jobs.enqueue(self.gh.key, 3, 82, acknowledge=True)
        jobs.set_tier(self.gh.key, 3, 82, "help")
        jobs.finish(jobs.next(), "failed")
        ack = acknowledge.Acknowledgement(jobs, self.state)
        with patch.object(acknowledge, "run_forge", return_value=dict(headSha=HEAD)), \
                patch.object(acknowledge, "remove"), \
                patch.object(acknowledge, "post_comment", side_effect=lambda *args: posted.append(args[3])):
            ack.finished(self.gh, 3, 82, "failed")
        self.assertEqual([re.findall(r"`(/[\w-]+)`", text) for text in posted], [["/help"] * 2])

    def test_a_later_max_review_does_not_read_the_command_or_the_help_comment_as_conversation(self):
        notes = [dict(body="/help"), dict(body=discovery.help_text()), dict(body="Please look at the retry path."),
                 dict(body="/help me find the retry path")]
        # A comment that only starts with the command is discussion, and is kept.
        self.assertEqual(context.earlier_comments(notes), "## Comment 1\nPlease look at the retry path.\n\n"
                                                          "## Comment 2\n/help me find the retry path")


if __name__ == "__main__":
    unittest.main()
