import argparse
from contextlib import closing, contextmanager, nullcontext
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

from cursor_pr import cli, poll
from cursor_pr.config import project_from
from cursor_pr.poll import Jobs, cycle, instance_lock, main, run_review, supervised
from cursor_pr.source import Change, ReviewError

HOLDER = """from pathlib import Path
import sys
from cursor_pr.poll import instance_lock
with instance_lock(Path(sys.argv[1])):
    pass
"""
# A process that holds a lock on the file it is given and then waits. Whether it has ended is read
# from the lock, which a process that was killed has released even before anyone has waited for it.
BUSY = """import fcntl, sys, time
held = open(sys.argv[1], "w")
fcntl.flock(held, fcntl.LOCK_EX)
open(sys.argv[1] + ".ready", "w").close()
time.sleep(120)
"""
# A poller that holds the instance lock and is waiting for a review when it is killed.
POLLER = f"""from pathlib import Path
import sys
from cursor_pr import poll
state = Path(sys.argv[1])
with poll.instance_lock(state):
    poll.supervised([sys.executable, "-c", {BUSY!r}, str(state / "review")])
"""


def gone(path, within=15):
    # Whether the process that locked this file has ended, however it ended.
    import fcntl
    deadline = time.monotonic() + within
    while True:
        with open(path, "a") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                pass
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


def started(path, within=15):
    deadline = time.monotonic() + within
    while not Path(str(path) + ".ready").exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} was never started")
        time.sleep(0.02)


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
                return b""
            cli.settle(args, cli.review_outcome(json.loads(answered)))
            return answered
        with patch("cursor_pr.poll.poll_project"), patch("cursor_pr.poll.supervised", side_effect=child) as run:
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
                         ["project", "number", "comment", "status", "attempts", "head", "prepared"])
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
        with patch("cursor_pr.poll.supervised", return_value=self.REVIEWED) as run:
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
            with self.subTest(report=report), patch("cursor_pr.poll.supervised") as run:
                run.return_value = json.dumps(report).encode()
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

    def test_retry_uses_the_same_prepared_report_without_reviewing_again(self):
        head = "b" * 40
        address = "acme/api#3"
        url = "https://github.com/acme/api/pull/3"
        args = cli.parser().parse_args(["--change", address, "--expected-change-url", url, "--post", *self.options])
        args.max_input_tokens = 6000
        with patch("cursor_pr.cli.forge_change", return_value=Change(head, [], url)), \
                patch("cursor_pr.cli.publish") as publish:
            first, code = cli.run_request(args)
            self.assertEqual(code, 0)
            # The forge accepted the report, but the review never settled its request row.
            with self.assertLogs("cursor_pr.poll", level="WARNING"):
                jobs = Jobs(self.state)
            jobs.next()
            with patch("cursor_pr.cli.pack", side_effect=AssertionError("review repeated")):
                self.assertEqual(cli.run_request(args), (first, code))
            self.assertEqual(publish.call_args_list[0], publish.call_args_list[1])
        cli.settle(args, "reviewed")
        with closing(sqlite3.connect(self.queue)) as db:
            self.assertEqual(db.execute("SELECT status, prepared FROM requests").fetchone(), ("reviewed", None))

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

    def test_failure_is_settled_before_its_diagnostic_can_fail(self):
        class Gone(io.StringIO):
            # Nobody reads the review's diagnostics any more.
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

        project = dict(provider="github", url="https://github.com/acme/api", checkout=str(self.state),
                       requester_ids=[7])
        argv = ["cursor-review", "--change", "acme/api#3", "--expected-change-url",
                project_from(project).change_url(3), "--max-input-tokens", "6000", *self.options]
        with patch("sys.argv", argv), patch("cursor_pr.cli.run_request", side_effect=ReviewError("Forge is down")), \
                patch.dict(os.environ, {"CURSOR_REVIEW_POLLER_CHILD": "1", "REVIEW_STATE_DIR": str(self.state),
                                        "REVIEW_FORGE_PROJECT": json.dumps(project)}), \
                patch("sys.stderr", Gone()), self.assertRaises(BrokenPipeError):
            cli.main()
        self.assertEqual(self.row()[0], "failed")  # Not left running for a restart to repeat.

    def stop(self, cleanup="pass"):
        # The review child as the poller starts it, waiting on a command, when its supervisor asks it
        # to stop. The command holds a lock while it runs and the review says when its cleanup ran.
        waiting = f"""import subprocess, sys
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
        {cleanup}
def wait(args):
    subprocess.run([sys.executable, "-c", {BUSY!r}, str(state / "command")])
sys.argv[0] = "cursor-review"
with patch("cursor_pr.cli.tempfile.TemporaryDirectory", side_effect=settings), \\
        patch("cursor_pr.cli.run_request", side_effect=wait):
    sys.exit(cli.main())
"""
        project = dict(provider="github", url="https://github.com/acme/api", checkout=str(self.state),
                       requester_ids=[7])
        review = subprocess.Popen(
            [sys.executable, "-c", waiting, "--change", "acme/api#3", "--expected-change-url",
             project_from(project).change_url(3), "--max-input-tokens", "6000", *self.options],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, CURSOR_REVIEW_POLLER_CHILD="1", REVIEW_STATE_DIR=str(self.state),
                     REVIEW_FORGE_PROJECT=json.dumps(project)))
        self.addCleanup(review.kill)
        started(self.state / "command")
        review.send_signal(signal.SIGTERM)
        out, err = review.communicate(timeout=20)
        self.assertEqual((review.returncode, out), (1, ""), err)
        self.assertTrue((self.state / "cleaned").exists())
        self.assertTrue(gone(self.state / "command"))  # The command it waited on was ended with it.
        self.assertEqual(self.row()[0], "started")  # For the poller to finish, or the next one to queue again.
        return err

    def test_review_asked_to_stop_cleans_up_and_leaves_its_row_running(self):
        self.assertIn("cursor-review: stopped by its supervisor\n", self.stop())

    def test_review_whose_cleanup_fails_while_it_stops_is_still_an_interrupted_one(self):
        # Not a review that ran and failed: its row must stay for the retry.
        err = self.stop('raise OSError("Directory not empty")')
        self.assertIn("cursor-review: stopped by its supervisor (Directory not empty)\n", err)

    def test_review_keeps_the_stop_request_to_itself(self):
        before = signal.getsignal(signal.SIGTERM)
        for options in ((), self.options):
            with self.subTest(options=options), patch("sys.stdout", new_callable=io.StringIO):
                self.enter(*options)
            self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_review_that_cannot_reach_its_row_still_reports_its_failure(self):
        code, _, err = self.enter("--queue", str(self.queue), "--request", self.KEY, "three", "81",
                                  failing=ReviewError("Forge is down"))
        self.assertEqual(code, 2)
        self.assertIn("Forge is down", err)
        self.assertIn("could not record how the review ended", err)
        self.assertEqual(self.row()[0], "started")


class SupervisorTests(unittest.TestCase):
    """The process between the poller and a review, run as the poller runs it, with real processes."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def supervise(self, command, seconds=60, keep=(), grace=None, number=None):
        # Returns the supervisor and the end of the lifeline that the poller holds.
        lifeline, hold = os.pipe()
        if number is not None:  # The lifeline under the number it would have in a poller with many files open.
            os.dup2(lifeline, number)
            os.close(lifeline)
            lifeline = number
        self.addCleanup(lambda: [os.close(fd) for fd in (hold,) if fd in self.open])
        self.open = {hold}
        argv = [str(lifeline), str(seconds), ",".join(map(str, keep)), *command]
        if grace is None:
            start = [sys.executable, "-I", str(poll.SUPERVISOR), *argv]
        else:  # The same code with a shorter wait between asking a review to stop and ending it.
            start = [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); import supervise\n"
                     f"supervise.GRACE_SECONDS = {grace}; sys.exit(supervise.main(sys.argv[1:]))",
                     str(poll.SUPERVISOR.parent), *argv]
        process = subprocess.Popen(start, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   pass_fds=(lifeline, *keep))
        os.close(lifeline)
        self.addCleanup(process.kill)
        return process, hold

    def let_go(self, hold):
        os.close(hold)
        self.open.discard(hold)

    def busy(self, name):
        return [sys.executable, "-c", BUSY, str(self.state / name)]

    def test_review_that_ends_passes_its_output_and_status_through(self):
        for code in (0, 2):
            with self.subTest(code=code):
                process, _ = self.supervise([sys.executable, "-c", f"import sys; print('report'); sys.exit({code})"])
                self.assertEqual((process.communicate(timeout=20), process.returncode), (("report\n", ""), code))

    def test_review_is_ended_when_its_poller_is_gone(self):
        process, hold = self.supervise(self.busy("review"))
        started(self.state / "review")
        self.let_go(hold)  # All a poller that was killed does.
        _, err = process.communicate(timeout=20)
        self.assertEqual((process.returncode, err),
                         (124, "cursor-review: the review was stopped: the poller that started it is gone\n"))
        self.assertTrue(gone(self.state / "review"))

    def test_lifeline_may_have_any_descriptor_number(self):
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 2048:
            self.skipTest("cannot open a descriptor numbered above 1024 here")
        resource.setrlimit(resource.RLIMIT_NOFILE, (2048, hard))
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        process, hold = self.supervise(self.busy("review"), number=1500)
        started(self.state / "review")
        self.assertIsNone(process.poll())  # Still watching, not ended by a wait that refuses the number.
        self.let_go(hold)
        _, err = process.communicate(timeout=20)
        self.assertEqual((process.returncode, err),
                         (124, "cursor-review: the review was stopped: the poller that started it is gone\n"))
        self.assertTrue(gone(self.state / "review"))

    def test_review_is_ended_when_the_supervisor_cannot_watch(self):
        # As when it is given a lifeline it cannot wait on: here one that is not open.
        process = subprocess.Popen([sys.executable, "-I", str(poll.SUPERVISOR), "987", "60", "", *self.busy("review")],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.kill)
        _, err = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 124)
        self.assertIn("cursor-review: the review was stopped: its supervisor failed:", err)
        self.assertTrue(gone(self.state / "review"))

    def test_review_is_ended_when_its_time_is_up(self):
        process, _ = self.supervise(self.busy("review"), seconds=0.5)
        _, err = process.communicate(timeout=20)
        self.assertEqual((process.returncode, err),
                         (124, "cursor-review: the review was stopped: it was still running after 0.5 seconds\n"))
        self.assertTrue(gone(self.state / "review"))

    def test_review_is_ended_when_its_supervisor_is_told_to_stop(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=number.name):
                process, _ = self.supervise(self.busy(number.name))
                started(self.state / number.name)
                process.send_signal(number)
                _, err = process.communicate(timeout=20)
                self.assertEqual((process.returncode, err),
                                 (124, "cursor-review: the review was stopped: its supervisor was told to stop\n"))
                self.assertTrue(gone(self.state / number.name))

    def test_commands_a_review_started_are_ended_with_it(self):
        # The review starts a command and then: waits on it, ends without it, or is killed.
        review = ("import os, signal, subprocess, sys, time\n"
                  f"subprocess.Popen([sys.executable, '-c', {BUSY!r}, sys.argv[1]])\n"
                  "while not os.path.exists(sys.argv[1] + '.ready'): time.sleep(0.02)\n")
        for name, then, how in (("waits", "time.sleep(120)", "poller"), ("ends", "", None),
                                ("killed", "os.kill(os.getpid(), signal.SIGKILL)", None)):
            with self.subTest(review=name):
                process, hold = self.supervise([sys.executable, "-c", review + then, str(self.state / name)])
                started(self.state / name)
                if how:
                    self.let_go(hold)
                process.communicate(timeout=20)
                self.assertEqual(process.returncode, {"waits": 124, "ends": 0, "killed": 124}[name])
                self.assertTrue(gone(self.state / name))

    def test_review_that_ignores_the_request_to_stop_is_ended_anyway(self):
        stubborn = "import signal, sys\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + BUSY
        process, hold = self.supervise([sys.executable, "-c", stubborn, str(self.state / "review")], grace=0.3)
        started(self.state / "review")
        self.let_go(hold)
        process.communicate(timeout=20)
        self.assertEqual(process.returncode, 124)
        self.assertTrue(gone(self.state / "review"))

    def test_review_asked_to_stop_is_given_time_to_leave(self):
        polite = ("import signal, sys, time\nfrom pathlib import Path\n"
                  "def leave(*received):\n    Path(sys.argv[1] + '.left').touch(); sys.exit(1)\n"
                  "signal.signal(signal.SIGTERM, leave)\nPath(sys.argv[1] + '.ready').touch()\ntime.sleep(120)\n")
        process, hold = self.supervise([sys.executable, "-c", polite, str(self.state / "review")])
        started(self.state / "review")
        began = time.monotonic()
        self.let_go(hold)
        process.communicate(timeout=20)
        self.assertTrue((self.state / "review.left").exists())
        self.assertLess(time.monotonic() - began, 4)  # It left at once, so the wait for it ended at once.

    def test_lock_is_held_until_the_review_is_gone(self):
        # The supervisor and the review both hold the poller's lock, and nothing the review starts does.
        review = ("import os, subprocess, sys, time\n"
                  "open(sys.argv[1] + '.group', 'w').write(str(os.getpgrp()))\n"
                  f"subprocess.Popen([sys.executable, '-c', {BUSY!r}, sys.argv[1]])\n"
                  "time.sleep(120)\n")

        def end_review():  # This test leaves the review without its supervisor, so it ends it itself.
            try:
                os.killpg(int((self.state / "command.group").read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.addCleanup(end_review)
        with open(self.state / "lock", "w") as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            process, hold = self.supervise([sys.executable, "-c", review, str(self.state / "command")],
                                           keep=(lock.fileno(),))
        started(self.state / "command")
        self.assertFalse(gone(self.state / "lock", within=0.5))  # The poller has closed its own descriptor.
        process.kill()  # Even with its supervisor killed, the review still holds the lock while it runs.
        process.wait()
        self.assertFalse(gone(self.state / "lock", within=0.5))
        self.assertFalse(gone(self.state / "command", within=0))

    def test_lock_is_free_once_the_supervisor_has_ended_the_review(self):
        with open(self.state / "lock", "w") as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            process, hold = self.supervise(self.busy("review"), keep=(lock.fileno(),))
        started(self.state / "review")
        self.let_go(hold)
        process.communicate(timeout=20)
        self.assertTrue(gone(self.state / "lock", within=2))
        self.assertTrue(gone(self.state / "review", within=0))  # Not before the review was gone.


class SupervisedTests(unittest.TestCase):
    """How the poller runs a review under the supervisor."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def test_poller_reads_what_the_review_printed(self):
        self.assertEqual(poll.supervised([sys.executable, "-c", "print('report')"], cwd=self.state), b"report\n")

    def test_review_past_its_time_is_ended_and_reported_as_failed(self):
        project = project_from(dict(provider="github", url="https://github.com/acme/api", requester_ids=[7],
                                    checkout=str(self.state)))
        busy = [sys.executable, "-c", BUSY, str(self.state / "review")]
        with patch("cursor_pr.poll.REVIEW_SECONDS", 0.5), patch("cursor_pr.poll.sys.executable", sys.executable), \
                patch("cursor_pr.poll.supervised", side_effect=lambda argv, **options: supervised(busy)):
            self.assertEqual(run_review(project, 3, self.state), "failed")
        self.assertTrue(gone(self.state / "review"))

    def test_poller_stops_waiting_for_a_supervisor_that_does_not_end(self):
        stuck = self.state / "stuck.py"
        stuck.write_text("import time\ntime.sleep(120)\n")
        with patch("cursor_pr.poll.SUPERVISOR", stuck), patch("cursor_pr.poll.REVIEW_SECONDS", 0.3), \
                patch("cursor_pr.poll.STOP_SECONDS", 0.3):
            began = time.monotonic()
            self.assertIsNone(poll.supervised([sys.executable, "-c", "pass"]))
        self.assertLess(time.monotonic() - began, 10)

    def test_interrupted_poller_ends_the_review_before_it_goes_on(self):
        busy = [sys.executable, "-c", BUSY, str(self.state / "review")]

        def interrupt(self_, *args, **kwargs):
            started(self.state / "review")
            raise KeyboardInterrupt

        with patch("cursor_pr.poll.subprocess.Popen.communicate", autospec=True, side_effect=interrupt), \
                self.assertRaises(KeyboardInterrupt):
            poll.supervised(busy)
        self.assertTrue(gone(self.state / "review", within=0))  # Already ended when the interrupt gets out.

    def test_timed_out_review_is_logged(self):
        project = project_from(dict(provider="github", url="https://github.com/acme/api", requester_ids=[7],
                                    checkout=str(self.state)))
        with patch("cursor_pr.poll.supervised", return_value=None), \
                self.assertLogs("cursor_pr.poll", level="WARNING") as logged:
            self.assertEqual(run_review(project, 3, self.state, 81), "failed")
        self.assertIn("Review acme/api#3 did not end within", logged.output[0])


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

    def test_review_does_not_outlive_a_poller_that_is_killed(self):
        poller = subprocess.Popen([sys.executable, "-c", POLLER, str(self.state)], stderr=subprocess.PIPE, text=True)
        self.addCleanup(poller.kill)
        started(self.state / "review")
        self.assertIn("BlockingIOError", self.holder().stderr)
        poller.kill()  # Only the poller. Nothing tells the review or its supervisor.
        poller.wait()
        self.assertTrue(gone(self.state / "review"))  # The review was ended,
        deadline = time.monotonic() + 15
        while "BlockingIOError" in (other := self.holder()).stderr and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(other.returncode, 0, other.stderr)  # and only then was the lock free again.

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
