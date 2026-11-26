import argparse
from contextlib import closing, nullcontext
import errno
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from cursor_pr import cli
from cursor_pr.config import project_from
from cursor_pr.poll import Jobs, cycle, instance_lock, main, run_review
from cursor_pr.source import Change, ReviewError

HOLDER = """from pathlib import Path
import sys
from cursor_pr.poll import instance_lock
with instance_lock(Path(sys.argv[1])):
    pass
"""
# Stands in for a review that outlives its poller. It ends when it is released, and also when the test's
# directory is gone, so a test that fails before releasing it leaves no process behind.
ORPHAN = ("import sys, time\nfrom pathlib import Path\nrelease = Path(sys.argv[1])\n"
          "while not release.exists() and release.parent.exists(): time.sleep(0.05)")
# A poller that is killed while its review runs. The review is started the way run_review starts one.
KILLED = f"""from pathlib import Path
import os, subprocess, sys
from unittest.mock import patch
from cursor_pr import poll
from cursor_pr.config import project_from

state, release = Path(sys.argv[1]), Path(sys.argv[2])
with poll.instance_lock(state):
    def review(argv, timeout, **kwargs):
        subprocess.Popen([sys.executable, "-c", {ORPHAN!r}, str(release)], stderr=subprocess.DEVNULL, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b'{{"state":"reviewed", "publication":"posted-or-already-present"}}')
    with patch("cursor_pr.poll.subprocess.run", side_effect=review):
        poll.run_review(project_from(dict(provider="github", url="https://github.com/acme/api", requester_ids=[7],
                                           checkout=str(state))), 3, state)
os._exit(0)
"""


def child_of(state, job):
    # The options the poller gives the review child for a request, as the child's parser reads them.
    return argparse.Namespace(queue=state / "requests.sqlite3", request=[job[0], str(job[1]), str(job[2])])


class QueueRestartTests(unittest.TestCase):
    REVIEWED = b'{"state":"reviewed","publication":"posted-or-already-present"}'
    UNRECORDED = "marked failed, the head it started at was not recorded, a new /review retries it"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.gh, self.gl = [
            project_from(dict(provider=p, url=f"https://{p}.com/acme/{p}", requester_ids=[7],
                              checkout=tmp.name)) for p in ("github", "gitlab")]
        self.job = (self.gh.key, 3, 81)

    def sql(self, statement, *parameters):
        with closing(sqlite3.connect(self.state / "requests.sqlite3")) as db, db:
            return db.execute(statement, parameters).fetchall()

    def restart(self):
        # A new poller reports every row it queues again or gives up on.
        with self.assertLogs("cursor_pr.poll", level="WARNING") as logged:
            return Jobs(self.state), [line.split("cursor_pr.poll:", 1)[1] for line in logged.output]

    def run_cycle(self, jobs, head="a" * 40, answered=REVIEWED):
        # One cycle with the review child replaced by one that finds the change at this head and, when
        # it is let through, ends as its report says.
        def child(argv, **kwargs):
            at = argv.index("--request")
            args = argparse.Namespace(queue=Path(argv[argv.index("--queue") + 1]), request=argv[at + 1:at + 4])
            try:
                cli.claim_head(args, head)
            except ReviewError:
                cli.settle(args, "failed")
                return Mock(stdout=b"")
            cli.settle(args, cli.review_outcome(json.loads(answered)))
            return Mock(stdout=answered)
        with patch("cursor_pr.poll.poll_project"), patch("cursor_pr.poll.subprocess.run", side_effect=child) as run:
            cycle({self.gh.key: self.gh}, self.state, jobs, {self.gh.key: None})
            return run.call_args.args[0]

    def test_interrupted_review_is_requeued_once_and_then_runs(self):
        jobs = Jobs(self.state)
        jobs.enqueue(*self.job)
        self.assertEqual(jobs.next(), self.job)  # The poller dies while the review runs.
        jobs, logged = self.restart()
        self.assertEqual(logged, [f"Review of {self.gh.key} #3 comment 81 was interrupted by a restart; queued again"])
        self.assertEqual(self.sql("SELECT number, status, attempts FROM requests"), [(3, "queued", 1)])
        ran = []
        with patch("cursor_pr.poll.poll_project"):
            cycle({self.gh.key: self.gh}, self.state, jobs, {self.gh.key: None},
                  lambda project, number, state: ran.append(number) or "reviewed")
        self.assertEqual(ran, [3])
        self.assertEqual(self.sql("SELECT status, attempts FROM requests"), [("reviewed", 2)])
        self.assertIsNone(Jobs(self.state).next())

    def test_interrupted_review_is_not_requeued_beyond_the_attempt_limit(self):
        jobs = Jobs(self.state)
        jobs.enqueue(*self.job)
        jobs.enqueue(self.gl.key, 4, 82)
        self.assertEqual(jobs.next(), self.job)  # The first attempt dies with the poller.
        self.restart()
        jobs = Jobs(self.state)
        self.assertEqual(jobs.next(), self.job)  # So does the retry.
        jobs, logged = self.restart()
        self.assertEqual(logged, [f"Review of {self.gh.key} #3 comment 81 was interrupted by a restart; "
                                  "marked failed, a new /review retries it"])
        self.assertEqual(self.sql("SELECT number, status, attempts FROM requests ORDER BY rowid"),
                         [(3, "failed", 2), (4, "queued", 0)])
        self.assertEqual(jobs.next(), (self.gl.key, 4, 82))  # A failed row is never requeued.
        jobs.finish((self.gl.key, 4, 82), "failed")
        self.assertIsNone(Jobs(self.state).next())
        self.assertEqual(self.sql("SELECT status FROM requests WHERE number=3"), [("failed",)])

    def test_restart_leaves_queued_and_finished_rows_alone(self):
        jobs = Jobs(self.state)
        for number, status in enumerate(("queued", "ignored", "failed", "reviewed", "partial",
                                         "over-budget", "model-unavailable"), 3):
            jobs.enqueue(self.gh.key, number, 80, status)
        before = self.sql("SELECT * FROM requests")
        with self.assertNoLogs("cursor_pr.poll", level="WARNING"):
            restarted = Jobs(self.state)
            self.assertEqual(self.sql("SELECT * FROM requests"), before)
            self.assertEqual(restarted.next(), (self.gh.key, 3, 80))
            self.assertIsNone(restarted.next())

    def old_queue(self, *rows, attempts=""):
        with closing(sqlite3.connect(self.state / "requests.sqlite3")) as db, db:
            db.execute(f"""CREATE TABLE requests (project TEXT NOT NULL, number INTEGER NOT NULL,
                comment INTEGER NOT NULL, status TEXT NOT NULL, {attempts}PRIMARY KEY(project, number, comment))""")
            db.executemany("INSERT INTO requests (project, number, comment, status) VALUES (?,?,?,?)", rows)

    def test_queue_from_the_old_schema_migrates(self):
        self.old_queue((self.gh.key, 3, 81, "queued"), (self.gl.key, 4, 82, "started"))
        jobs, logged = self.restart()
        self.assertEqual(logged, [f"Review of {self.gl.key} #4 comment 82 was interrupted by a restart; {self.UNRECORDED}"])
        self.assertEqual([row[1] for row in self.sql("PRAGMA table_info(requests)")],
                         ["project", "number", "comment", "status", "attempts", "head"])
        self.assertEqual(self.sql("SELECT number, status, attempts, head FROM requests"),
                         [(3, "queued", 0, ""), (4, "failed", 0, "")])
        jobs.enqueue(self.gh.key, 5, 83)
        self.assertEqual(jobs.next(), self.job)  # A failed row is never started again.
        jobs, logged = self.restart()  # The poller stopped before this review resolved a head.
        self.assertEqual(logged, [f"Review of {self.gh.key} #3 comment 81 was interrupted by a restart; "
                                  "queued again"])
        self.assertEqual(jobs.next(), self.job)
        jobs.finish(self.job, "reviewed")
        self.assertEqual(jobs.next(), (self.gh.key, 5, 83))
        self.assertIsNone(jobs.next())
        self.assertEqual(self.sql("SELECT number, status, attempts FROM requests ORDER BY rowid"),
                         [(3, "reviewed", 2), (4, "failed", 0), (5, "started", 1)])

    def test_queue_that_counts_starts_but_keeps_no_head_migrates_the_same_way(self):
        self.old_queue((self.gl.key, 4, 82, "started"), attempts="attempts INTEGER NOT NULL DEFAULT 0, ")
        _, logged = self.restart()
        self.assertEqual(logged, [f"Review of {self.gl.key} #4 comment 82 was interrupted by a restart; {self.UNRECORDED}"])
        self.assertEqual(self.sql("SELECT status, head FROM requests"), [("failed", "")])

    def test_migration_that_stops_halfway_leaves_the_old_queue(self):
        self.old_queue((self.gl.key, 4, 82, "started"))

        class Stopped(Exception):
            pass

        real = sqlite3.connect

        class Halfway(sqlite3.Connection):
            def execute(self, statement, *args):
                if statement.startswith("UPDATE requests SET status='failed'"):
                    raise Stopped
                return super().execute(statement, *args)
        # The poller stops after the columns are added and before the running review is given up on.
        with patch("cursor_pr.poll.sqlite3.connect", side_effect=lambda path: real(path, factory=Halfway)), \
                self.assertRaises(Stopped):
            Jobs(self.state)
        self.assertEqual([row[1] for row in self.sql("PRAGMA table_info(requests)")],
                         ["project", "number", "comment", "status"])  # Nothing of it was kept.
        _, logged = self.restart()
        self.assertEqual(logged, [f"Review of {self.gl.key} #4 comment 82 was interrupted by a restart; {self.UNRECORDED}"])

    def test_review_child_is_given_the_queue_and_its_request(self):
        jobs = Jobs(self.state)
        jobs.enqueue(*self.job)
        with self.assertNoLogs("cursor_pr.poll", level="WARNING"):
            argv = self.run_cycle(jobs)
        self.assertEqual(argv[argv.index("--queue") + 1], str(self.state / "requests.sqlite3"))
        at = argv.index("--request")
        self.assertEqual(argv[at + 1:at + 4], [self.gh.key, "3", "81"])
        self.assertEqual(self.sql("SELECT status, attempts, head FROM requests"), [("reviewed", 1, "a" * 40)])

    def test_review_started_outside_the_queue_is_given_neither(self):
        with patch("cursor_pr.poll.subprocess.run", return_value=Mock(stdout=self.REVIEWED)) as run:
            self.assertEqual(run_review(self.gh, 3, self.state), "reviewed")
        self.assertNotIn("--queue", run.call_args.args[0])
        self.assertNotIn("--request", run.call_args.args[0])

    def test_repeat_stays_at_the_head_of_its_interrupted_start(self):
        jobs = Jobs(self.state)
        jobs.enqueue(*self.job)
        jobs.next()  # The poller stops while the review runs.
        cli.claim_head(child_of(self.state, self.job), "a" * 40)  # Its review child had got this far.
        jobs, logged = self.restart()
        self.assertEqual(logged, [f"Review of {self.gh.key} #3 comment 81 was interrupted by a restart; queued again"])
        self.assertEqual(self.sql("SELECT status, attempts, head FROM requests"), [("queued", 1, "a" * 40)])
        with self.assertNoLogs("cursor_pr.poll", level="WARNING"):
            self.run_cycle(jobs)
        self.assertEqual(self.sql("SELECT status, attempts, head FROM requests"), [("reviewed", 2, "a" * 40)])

    def test_repeat_at_another_head_is_refused_and_reported(self):
        jobs = Jobs(self.state)
        jobs.enqueue(*self.job)
        jobs.next()
        cli.claim_head(child_of(self.state, self.job), "a" * 40)
        jobs, _ = self.restart()
        with self.assertLogs("cursor_pr.poll", level="WARNING") as logged:
            self.run_cycle(jobs, head="b" * 40)  # The change has moved on: the child refuses.
        self.assertEqual([line.split("cursor_pr.poll:", 1)[1] for line in logged.output],
                         [f"Review of {self.gh.key} #3 comment 81 was not completed by its retry; "
                          "a new /review retries it"])
        self.assertEqual(self.sql("SELECT status, attempts, head FROM requests"), [("failed", 2, "a" * 40)])
        self.assertIsNone(jobs.next())

    def test_each_request_keeps_its_own_head(self):
        jobs = Jobs(self.state)
        jobs.enqueue(*self.job)
        jobs.enqueue(self.gh.key, 3, 82)
        self.run_cycle(jobs, head="f" * 40)  # Both run. Then a push, and the poller stops in a third.
        jobs.enqueue(self.gh.key, 3, 83)
        self.assertEqual(jobs.next(), (self.gh.key, 3, 83))
        jobs, _ = self.restart()
        self.assertEqual(self.sql("SELECT comment, status, head FROM requests ORDER BY rowid"),
                         [(81, "reviewed", "f" * 40), (82, "reviewed", "f" * 40), (83, "queued", "")])
        with self.assertNoLogs("cursor_pr.poll", level="WARNING"):
            self.run_cycle(jobs, head="b" * 40)  # Not held to the head of the requests before it.
        self.assertEqual(self.sql("SELECT status, head FROM requests WHERE comment=83"), [("reviewed", "b" * 40)])

    def outlived(self, starts, outcome):
        # What a restart does with a request whose review finished after its poller stopped.
        (self.state / "requests.sqlite3").unlink(missing_ok=True)
        jobs = Jobs(self.state)
        jobs.enqueue(*self.job)
        jobs.next()
        if starts == 2:  # The first review was cut off with its poller. It is the retry that outlives one.
            with self.assertLogs("cursor_pr.poll", level="WARNING"):
                jobs = Jobs(self.state)
            jobs.next()
        cli.claim_head(child_of(self.state, self.job), "a" * 40)
        cli.settle(child_of(self.state, self.job), outcome)  # The review child's last act.
        with self.assertNoLogs("cursor_pr.poll", level="WARNING"):  # Nothing is left for a restart to settle.
            jobs = Jobs(self.state)
        return self.sql("SELECT status, attempts FROM requests"), jobs.next()

    def test_review_that_outlived_its_poller_is_kept_as_it_ended(self):
        for starts in (1, 2):  # With "failed": the poller is killed while the review publishes, and that fails.
            for outcome in ("reviewed", "partial", "over-budget", "failed"):
                with self.subTest(starts=starts, outcome=outcome):
                    self.assertEqual(self.outlived(starts, outcome), ([(outcome, starts)], None))

    def test_first_outcome_of_a_review_stands(self):
        jobs = Jobs(self.state)
        jobs.enqueue(*self.job)
        jobs.next()
        cli.settle(child_of(self.state, self.job), "reviewed")
        cli.settle(child_of(self.state, self.job), "failed")  # As when the report could not be printed.
        jobs.finish(self.job, "failed")  # As when the poller could not read it.
        self.assertEqual(self.sql("SELECT status FROM requests"), [("reviewed",)])

    def test_review_that_never_settled_is_finished_by_the_poller(self):
        jobs = Jobs(self.state)
        jobs.enqueue(*self.job)
        jobs.next()
        with self.assertNoLogs("cursor_pr.poll", level="WARNING"):  # A first start that failed is not a retry.
            jobs.finish(self.job, "failed")
        self.assertEqual(self.sql("SELECT status FROM requests"), [("failed",)])

    def test_the_outcome_the_review_child_records_is_the_one_the_poller_keeps(self):
        for report in ({"state": "reviewed", "publication": "posted-or-already-present"}, {"state": "reviewed"},
                       {"state": "model-unavailable", "publication": "posted-or-already-present"},
                       {"state": "partial", "publication": "failed"}):
            with self.subTest(report=report), patch("cursor_pr.poll.subprocess.run") as run:
                run.return_value.stdout = json.dumps(report).encode()
                self.assertEqual(run_review(self.gh, 3, self.state), cli.review_outcome(report))


class RequestRowTests(unittest.TestCase):
    """The review child's side of the request's row."""
    KEY = "github/github.com/acme/api"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.queue = self.state / "requests.sqlite3"
        jobs = Jobs(self.state)
        jobs.enqueue(self.KEY, 3, 81)
        jobs.next()
        self.options = ("--queue", str(self.queue), "--request", self.KEY, "3", "81")

    def row(self):
        with closing(sqlite3.connect(self.queue)) as db:
            return db.execute("SELECT status, head FROM requests").fetchone()

    def review(self, *options):
        # The forge is replaced: the change is at head b whatever was asked for.
        change = Change("b" * 40, [], "https://github.com/acme/api/pull/3")
        args = cli.parser().parse_args(["--change", "acme/api#3", "--expected-change-url", change.identity,
                                        "--estimate", "--cache-dir", str(self.state / "cache"), *options])
        args.max_input_tokens = 6000
        with patch("cursor_pr.cli.forge_change", return_value=change):
            return cli.execute(args)

    def test_review_records_its_head_in_its_row(self):
        self.assertEqual(self.review(*self.options)[0]["state"], "estimate")
        self.assertEqual(self.row(), ("started", "b" * 40))
        self.assertEqual(self.review(*self.options)[0]["state"], "estimate")  # A repeat at the same head.

    def test_repeat_at_another_head_is_refused_and_leaves_the_row_as_it_was(self):
        cli.claim_head(child_of(self.state, (self.KEY, 3, 81)), "a" * 40)
        with self.assertRaises(ReviewError) as refused:
            self.review(*self.options)
        self.assertEqual(str(refused.exception),
                         f"Change is at {'b' * 40}, not at {'a' * 40}, the head of its interrupted start")
        self.assertEqual(self.row(), ("started", "a" * 40))

    def test_review_of_a_request_that_is_not_running_is_refused(self):
        for options in (("--queue", str(self.queue), "--request", self.KEY, "3", "82"),
                        ("--queue", str(self.state / "absent.sqlite3"), "--request", self.KEY, "3", "81")):
            with self.subTest(options=options), self.assertRaises(ReviewError):
                self.review(*options)
        self.assertEqual(self.row(), ("started", ""))
        self.assertFalse((self.state / "absent.sqlite3").exists())  # A wrong path is not made into a queue.

    def test_queue_and_request_go_together(self):
        for options in (("--queue", str(self.queue)), ("--request", self.KEY, "3", "81")):
            with self.subTest(options=options), self.assertRaises(ReviewError):
                self.review(*options)

    def enter(self, *options, failing=None, leaving=None):
        # The review child's entry point, as the poller starts it, with the review itself replaced.
        project = dict(provider="github", url="https://github.com/acme/api", checkout=str(self.state),
                       requester_ids=[7])
        argv = ["cursor-review", "--change", "acme/api#3", "--expected-change-url",
                project_from(project).change_url(3), "--max-input-tokens", "6000", *options]
        with patch("sys.argv", argv), patch("cursor_pr.cli.run_request", side_effect=failing, return_value=(
                {"state": "reviewed", "publication": "posted-or-already-present"}, 0)) as request, \
                patch("cursor_pr.cli.tempfile.TemporaryDirectory", side_effect=leaving or tempfile.TemporaryDirectory), \
                patch("cursor_pr.cli.os.setpgrp"), \
                patch.dict(os.environ, {"CURSOR_REVIEW_POLLER_CHILD": "1", "REVIEW_STATE_DIR": str(self.state),
                                        "REVIEW_FORGE_PROJECT": json.dumps(project)}), \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            code = cli.main()
        return code, request, err.getvalue()

    def test_review_settles_its_row_before_its_report_can_fail(self):
        class Gone:
            # The poller that would have read the report has been killed.
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self):
                raise BrokenPipeError(32, "Broken pipe")

        with patch("sys.stdout", Gone()):
            code, request, err = self.enter(*self.options)
        request.assert_called_once()
        self.assertEqual((code, "Broken pipe" in err), (2, True))
        self.assertEqual(self.row()[0], "reviewed")  # Not a failed review.

    def test_review_settles_its_row_before_its_cleanup_can_fail(self):
        state = self.state  # A directory of the test's own, so nothing lands in the shared temporary one.

        class Stuck:
            # The settings directory of the review, which cannot be removed afterwards.
            def __enter__(self):
                return str(state)

            def __exit__(self, *raised):
                raise OSError("Directory not empty")

        code, request, err = self.enter(*self.options, leaving=lambda **kwargs: Stuck())
        request.assert_called_once()
        self.assertEqual((code, "Directory not empty" in err), (2, True))
        self.assertEqual(self.row()[0], "reviewed")

    def test_review_that_fails_settles_its_row_as_failed(self):
        code, request, err = self.enter(*self.options, failing=ReviewError("Forge is down"))
        request.assert_called_once()
        self.assertEqual((code, "Forge is down" in err), (2, True))
        self.assertEqual(self.row()[0], "failed")

    def test_request_without_a_queue_is_refused_cleanly(self):
        # The review is replaced here. The real one refuses these options before any work, like this.
        code, _, err = self.enter("--request", self.KEY, "3", "81",
                                  failing=ReviewError("--queue and --request go together and require --change"))
        self.assertEqual((code, err), (2, "cursor-review: --queue and --request go together and require --change\n"))
        self.assertEqual(self.row()[0], "started")

    WAITING = """
    subprocess.run([sys.executable, "-c", "import sys, time; open(sys.argv[1], 'w').close(); time.sleep(60)",
                    str(state / "waiting")])"""

    def hang(self, review, cleanup_seconds=30, waiting=None):
        # The review child as the poller starts it, with a time limit of one second and a review that
        # waits on a command for a minute. The command says when it is running and the review says
        # when its cleanup ran.
        hung = f"""import subprocess, sys, time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from cursor_pr import cli
state = Path({str(self.state)!r})
@contextmanager
def settings(**kwargs):
    try:
        yield str(state)
    finally:
        (state / "cleaned").touch()
        {review}
def wait(args):{waiting or self.WAITING}
sys.argv[0] = "cursor-review"
with patch("cursor_pr.cli.QUEUED_REVIEW_SECONDS", 1), patch("cursor_pr.cli.CLEANUP_SECONDS", {cleanup_seconds}), \\
        patch("cursor_pr.cli.tempfile.TemporaryDirectory", side_effect=settings), \\
        patch("cursor_pr.cli.run_request", side_effect=wait):
    sys.exit(cli.main())
"""
        started = time.monotonic()
        ended = subprocess.run([sys.executable, "-c", hung, "--change", "acme/api#3", "--expected-change-url",
                                "https://github.com/acme/api/pull/3", "--max-input-tokens", "6000", *self.options],
                               capture_output=True, text=True, timeout=30,
                               env=dict(os.environ, CURSOR_REVIEW_POLLER_CHILD="1", REVIEW_STATE_DIR=str(self.state)))
        return ended, time.monotonic() - started

    def test_queued_review_that_hangs_gives_up_cleans_up_and_leaves_its_row_running(self):
        ended, took = self.hang("pass")
        self.assertEqual(ended.returncode, 1, ended.stderr)
        self.assertIn("cursor-review: still running after 1 seconds, giving up", ended.stderr)
        self.assertLess(took, 20)  # The command it waited on for a minute was killed, not waited for.
        self.assertTrue((self.state / "waiting").exists())
        self.assertTrue((self.state / "cleaned").exists())
        self.assertEqual(self.row()[0], "started")  # For the next poller to queue again or give up on.

    def test_queued_review_whose_cleanup_hangs_too_is_ended_outright(self):
        ended, took = self.hang("time.sleep(60)", cleanup_seconds=1)
        self.assertEqual(ended.returncode, -signal.SIGKILL, ended.stderr)
        self.assertLess(took, 20)
        self.assertEqual(self.row()[0], "started")

    def test_queued_review_stuck_where_no_handler_runs_is_ended_from_outside(self):
        # As in a native call that never returns to the interpreter: the alarm is never delivered.
        ended, took = self.hang("pass", cleanup_seconds=1, waiting="""
    import signal
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    time.sleep(60)""")
        self.assertEqual(ended.returncode, -signal.SIGKILL, ended.stderr)
        self.assertNotIn("giving up", ended.stderr)  # It never got to unwind.
        self.assertLess(took, 20)
        self.assertEqual(self.row()[0], "started")

    def test_watchdog_ends_the_commands_the_review_was_waiting_on(self):
        # The review is stuck where no handler runs while a command of its own is running.
        ended, took = self.hang("pass", cleanup_seconds=1, waiting="""
    import os, signal
    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (state / "helper").write_text(f"{helper.pid} {os.getpgid(helper.pid)} {os.getpid()}")
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    time.sleep(60)""")
        self.assertEqual(ended.returncode, -signal.SIGKILL, ended.stderr)
        self.assertLess(took, 20)
        helper, group, review = map(int, (self.state / "helper").read_text().split())
        self.assertEqual(group, review)  # The command ran in the review's own group,
        self.assertNotEqual(group, os.getpgrp())  # which is not the one of whoever started the review.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(helper, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(helper, signal.SIGKILL)
            self.fail("The command outlived the review")

    def test_poller_does_not_wait_for_a_review_past_its_limits(self):
        from cursor_pr import poll
        self.assertGreater(poll.REVIEW_WAIT_SECONDS, cli.QUEUED_REVIEW_SECONDS + cli.CLEANUP_SECONDS)
        project = project_from(dict(provider="github", url="https://github.com/acme/api", requester_ids=[7],
                                    checkout=str(self.state)))
        with patch("cursor_pr.poll.subprocess.run", side_effect=subprocess.TimeoutExpired("review", 1)) as run, \
                self.assertLogs("cursor_pr.poll", level="WARNING") as logged:
            self.assertEqual(run_review(project, 3, self.state, 81), "failed")
        self.assertEqual(run.call_args.kwargs["timeout"], poll.REVIEW_WAIT_SECONDS)
        self.assertIn("Review acme/api#3 did not end within", logged.output[0])

    def test_watchdog_ends_with_the_review_and_never_holds_the_lock(self):
        with patch("cursor_pr.cli.subprocess.Popen", wraps=subprocess.Popen) as started, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.enter(*self.options)
        self.assertEqual(started.call_count, 1)
        self.assertNotIn("pass_fds", started.call_args.kwargs)  # Its descriptors are closed: no lock comes along.
        self.assertFalse(started.call_args.kwargs.get("close_fds") is False)
        self.assertEqual(self.row()[0], "reviewed")

    def test_watchdog_ends_when_its_review_is_killed(self):
        review = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        guard = subprocess.Popen([sys.executable, "-I", "-c", cli.WATCHDOG, "60", str(review.pid)], stdin=subprocess.PIPE)
        guard.stdin.close()  # What the review's death does to the pipe.
        self.assertEqual(guard.wait(timeout=20), 0)
        self.assertIsNone(review.poll())  # Not ended by a watchdog whose review is gone already.
        review.kill()
        review.wait()

    def test_review_outside_the_queue_has_no_limit_of_its_own(self):
        with patch("cursor_pr.cli.signal.alarm") as alarm, patch("cursor_pr.cli.subprocess.Popen") as watchdog, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.enter()
        alarm.assert_not_called()
        watchdog.assert_not_called()

    def test_review_that_cannot_reach_its_row_still_reports_its_failure(self):
        code, _, err = self.enter("--queue", str(self.queue), "--request", self.KEY, "three", "81",
                                  failing=ReviewError("Forge is down"))
        self.assertEqual(code, 2)
        self.assertIn("Forge is down", err)
        self.assertIn("could not record how the review ended", err)
        self.assertEqual(self.row()[0], "started")


class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / "projects.json").write_text("{}")
        patcher = patch("sys.argv", ["cursor-review-poll", "--state-dir", str(self.state)])
        patcher.start()
        self.addCleanup(patcher.stop)

    def holder(self):
        return subprocess.run([sys.executable, "-c", HOLDER, str(self.state)], capture_output=True, text=True)

    def test_second_poller_on_the_same_state_directory_is_refused(self):
        with instance_lock(self.state):
            other = self.holder()  # Another process gets the same answer.
            self.assertNotEqual(other.returncode, 0)
            self.assertIn("BlockingIOError", other.stderr)
            with patch("cursor_pr.poll.ConfigurationWatch") as watch, \
                 patch("sys.stderr", new_callable=io.StringIO) as err, self.assertRaises(SystemExit) as stopped:
                main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(err.getvalue(), "Another poller is already running on this state volume.\n")
        watch.assert_not_called()  # Refused before setup, credentials or the queue are touched.
        self.assertEqual(self.holder().returncode, 0)

    def test_lock_that_cannot_be_taken_stops_the_poller_with_its_reason(self):
        for reason in (OSError(errno.ENOLCK, "No locks available"), PermissionError(errno.EACCES, "Permission denied")):
            with self.subTest(reason=reason), patch("cursor_pr.poll.fcntl.flock", side_effect=reason), \
                    patch("cursor_pr.poll.ConfigurationWatch") as watch, \
                    patch("sys.stderr", new_callable=io.StringIO) as err, self.assertRaises(SystemExit) as stopped:
                main()
            self.assertEqual(stopped.exception.code, 2)
            self.assertEqual(err.getvalue(), f"Polling lock: {reason}\n")
            watch.assert_not_called()

    def test_review_child_holds_the_lock_until_it_exits(self):
        release = self.state / "release"
        self.addCleanup(release.touch)  # Before the review is started, so a failed assertion releases it too.
        killed = subprocess.run([sys.executable, "-c", KILLED, str(self.state), str(release)],
                                capture_output=True, text=True)
        self.assertEqual(killed.returncode, 0, killed.stderr)
        self.assertIn("BlockingIOError", self.holder().stderr)  # Only the poller was killed. Its review still runs.
        release.touch()
        deadline = time.monotonic() + 20
        while "BlockingIOError" in (other := self.holder()).stderr and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(other.returncode, 0, other.stderr)  # The review has exited.

    def test_lock_is_held_while_polling_and_released_on_exit(self):
        opened = []

        def cycle(projects, state, jobs, since, *args):
            self.assertIn("BlockingIOError", self.holder().stderr)
            opened.append(jobs.path)

        watch = Mock(enabled=False)
        watch.changed.side_effect = [False, True]
        with patch("cursor_pr.poll.ConfigurationWatch", return_value=watch), \
             patch("cursor_pr.poll.setup.apply_saved_setup"), \
             patch("cursor_pr.poll.providers.provider", return_value="cursor"), \
             patch("cursor_pr.poll.auth_mode", return_value="api-key"), \
             patch("cursor_pr.poll.setup.max_input_tokens", return_value=6000), \
             patch("cursor_pr.poll.history.interval", return_value=0), \
             patch("cursor_pr.poll.load_config", return_value={}), \
             patch("cursor_pr.poll.providers.cursor_account", side_effect=lambda: nullcontext()), \
             patch("cursor_pr.poll.forge_credentials", side_effect=lambda *args: nullcontext()), \
             patch("cursor_pr.poll.cycle", side_effect=cycle), patch("cursor_pr.poll.logging.basicConfig"):
            main()
        self.assertEqual(opened, [self.state.resolve() / "requests.sqlite3"])
        self.assertEqual(self.holder().returncode, 0)
        self.assertEqual((self.state / ".poller.lock").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
