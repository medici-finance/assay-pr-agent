from contextlib import closing, nullcontext
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from cursor_pr import cli
from cursor_pr.config import project_from
from cursor_pr.poll import Jobs, cycle, head_record, instance_lock, main, run_review, write_head_record
from cursor_pr.source import Change, ReviewError

HOLDER = """from pathlib import Path
import sys
from cursor_pr.poll import instance_lock
with instance_lock(Path(sys.argv[1])):
    pass
"""
ORPHAN = "import sys, time\nfrom pathlib import Path\nwhile not Path(sys.argv[1]).exists(): time.sleep(0.05)"
# A poller that is killed while its review runs. The review is started the way run_review starts one.
KILLED = f"""from pathlib import Path
import os, subprocess, sys
from unittest.mock import patch
from cursor_pr import poll
from cursor_pr.config import project_from

state, release = Path(sys.argv[1]), Path(sys.argv[2])
with poll.instance_lock(state):
    def review(argv, **kwargs):
        subprocess.Popen([sys.executable, "-c", {ORPHAN!r}, str(release)], stderr=subprocess.DEVNULL, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b'{{"state":"reviewed", "publication":"posted-or-already-present"}}')
    with patch("cursor_pr.poll.subprocess.run", side_effect=review):
        poll.run_review(project_from(dict(provider="github", url="https://github.com/acme/api", requester_ids=[7],
                                           checkout=str(state))), 3, state)
os._exit(0)
"""


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

    def sql(self, statement, *parameters):
        with closing(sqlite3.connect(self.state / "requests.sqlite3")) as db, db:
            return db.execute(statement, parameters).fetchall()

    def restart(self):
        # A new poller reports every row it queues again or gives up on.
        with self.assertLogs("cursor_pr.poll", level="WARNING") as logged:
            return Jobs(self.state), [line.split("cursor_pr.poll:", 1)[1] for line in logged.output]

    def run_cycle(self, jobs, answered=REVIEWED):
        # One cycle with the review child replaced by one that gets as far as recording its head.
        def child(argv, **kwargs):
            if "--record-head" in argv:
                cli.record_head(Path(argv[argv.index("--record-head") + 1]), "a" * 40)
            return Mock(stdout=answered)
        with patch("cursor_pr.poll.poll_project"), patch("cursor_pr.poll.subprocess.run", side_effect=child) as run:
            cycle({self.gh.key: self.gh}, self.state, jobs, {self.gh.key: None})
            argv = run.call_args.args[0]
        self.assertFalse(head_record(self.state).exists())  # A request that ended leaves no head behind.
        return argv

    def test_interrupted_review_is_requeued_once_and_then_runs(self):
        jobs = Jobs(self.state)
        jobs.enqueue(self.gh.key, 3, 81)
        self.assertEqual(jobs.next(), (self.gh.key, 3, 81))  # The poller dies while the review runs.
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
        jobs.enqueue(self.gh.key, 3, 81)
        jobs.enqueue(self.gl.key, 4, 82)
        self.assertEqual(jobs.next(), (self.gh.key, 3, 81))  # The first attempt dies with the poller.
        self.restart()
        jobs = Jobs(self.state)
        self.assertEqual(jobs.next(), (self.gh.key, 3, 81))  # So does the retry.
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

    def old_queue(self, *rows):
        with closing(sqlite3.connect(self.state / "requests.sqlite3")) as db, db:
            db.execute("""CREATE TABLE requests (project TEXT NOT NULL, number INTEGER NOT NULL,
                comment INTEGER NOT NULL, status TEXT NOT NULL, PRIMARY KEY(project, number, comment))""")
            db.executemany("INSERT INTO requests VALUES (?,?,?,?)", rows)

    def test_queue_from_the_old_schema_migrates(self):
        self.old_queue((self.gh.key, 3, 81, "queued"), (self.gl.key, 4, 82, "started"))
        jobs, logged = self.restart()
        self.assertEqual(logged, [f"Review of {self.gl.key} #4 comment 82 was interrupted by a restart; {self.UNRECORDED}"])
        self.assertEqual([row[1] for row in self.sql("PRAGMA table_info(requests)")],
                         ["project", "number", "comment", "status", "attempts"])
        self.assertEqual(self.sql("SELECT number, status, attempts FROM requests"),
                         [(3, "queued", 0), (4, "failed", 0)])
        jobs.enqueue(self.gh.key, 5, 83)
        self.assertEqual(jobs.next(), (self.gh.key, 3, 81))  # A failed row is never started again.
        self.assertEqual(head_record(self.state).read_text(), f"{self.gh.key} 3 81\n\n")
        jobs, logged = self.restart()  # The poller stopped before this review resolved a head.
        self.assertEqual(logged, [f"Review of {self.gh.key} #3 comment 81 was interrupted by a restart; "
                                  "queued again"])
        self.assertEqual(jobs.next(), (self.gh.key, 3, 81))
        jobs.finish((self.gh.key, 3, 81), "reviewed")
        self.assertEqual(jobs.next(), (self.gh.key, 5, 83))
        self.assertIsNone(jobs.next())
        self.assertEqual(self.sql("SELECT number, status, attempts FROM requests ORDER BY rowid"),
                         [(3, "reviewed", 2), (4, "failed", 0), (5, "started", 1)])

    def test_retry_never_takes_an_earlier_requests_head(self):
        jobs = Jobs(self.state)
        jobs.enqueue(self.gh.key, 3, 81)
        jobs.enqueue(self.gh.key, 3, 82)
        jobs.next()
        cli.record_head(head_record(self.state), "f" * 40)
        self.sql("UPDATE requests SET status='reviewed' WHERE comment=81")  # It ended while the poller was
        # stopping before it removed the head.
        self.assertEqual(jobs.next(), (self.gh.key, 3, 82))  # The poller stops again at once.
        self.assertEqual(head_record(self.state).read_text(), f"{self.gh.key} 3 82\n\n")
        jobs, logged = self.restart()
        self.assertEqual(logged, [f"Review of {self.gh.key} #3 comment 82 was interrupted by a restart; queued again"])
        with self.assertNoLogs("cursor_pr.poll", level="WARNING"):
            argv = self.run_cycle(jobs)
        self.assertNotIn("--expected-head", argv)
        self.assertIn("--record-head", argv)

    def given_up_when(self, damage):
        jobs = Jobs(self.state)
        jobs.enqueue(self.gh.key, 3, 81)
        jobs.next()
        damage()
        jobs, logged = self.restart()
        self.assertEqual(logged, [f"Review of {self.gh.key} #3 comment 81 was interrupted by a restart; "
                                  f"{self.UNRECORDED}"])
        self.assertEqual(self.sql("SELECT status FROM requests"), [("failed",)])
        self.assertIsNone(jobs.next())

    def test_started_request_whose_head_record_is_missing_is_given_up(self):
        self.given_up_when(lambda: head_record(self.state).unlink())

    def test_started_request_whose_head_record_is_another_requests_is_given_up(self):
        self.given_up_when(lambda: write_head_record(self.state, (self.gl.key, 4, 82), "f" * 40))

    def test_first_start_records_its_head_and_the_requests_end_removes_it(self):
        jobs = Jobs(self.state)
        jobs.enqueue(self.gh.key, 3, 81)
        with self.assertNoLogs("cursor_pr.poll", level="WARNING"):
            argv = self.run_cycle(jobs)
        self.assertEqual(argv[argv.index("--record-head") + 1], str(head_record(self.state)))
        self.assertNotIn("--expected-head", argv)
        self.assertEqual(self.sql("SELECT status, attempts FROM requests"), [("reviewed", 1)])

    def test_repeat_stays_at_the_head_of_its_interrupted_start(self):
        jobs = Jobs(self.state)
        jobs.enqueue(self.gh.key, 3, 81)
        jobs.next()  # The poller stops while the review runs.
        cli.record_head(head_record(self.state), "a" * 40)  # Its review child had got this far.
        jobs, logged = self.restart()
        self.assertEqual(logged, [f"Review of {self.gh.key} #3 comment 81 was interrupted by a restart; queued again"])
        with self.assertNoLogs("cursor_pr.poll", level="WARNING"):
            argv = self.run_cycle(jobs)
        self.assertEqual(argv[argv.index("--expected-head") + 1], "a" * 40)
        self.assertEqual(argv[argv.index("--record-head") + 1], str(head_record(self.state)))
        self.assertEqual(self.sql("SELECT status, attempts FROM requests"), [("reviewed", 2)])

    def outlived(self, starts, outcome):
        # What a restart does with a request whose review finished after its poller stopped.
        for path in (self.state / "requests.sqlite3", head_record(self.state)):
            path.unlink(missing_ok=True)
        jobs = Jobs(self.state)
        jobs.enqueue(self.gh.key, 3, 81)
        jobs.next()
        if starts == 2:  # The first review was cut off with its poller. It is the retry that outlives one.
            with self.assertLogs("cursor_pr.poll", level="WARNING"):
                jobs = Jobs(self.state)
            jobs.next()
        cli.record_head(head_record(self.state), "a" * 40)
        cli.record_outcome(head_record(self.state), outcome)  # The review child's last act.
        jobs, logged = self.restart()
        return (self.sql("SELECT status, attempts FROM requests"), logged, head_record(self.state).exists(), jobs.next())

    def test_review_that_outlived_its_poller_is_kept_as_it_ended(self):
        for outcome in ("reviewed", "partial", "over-budget"):
            with self.subTest(outcome=outcome):
                self.assertEqual(self.outlived(2, outcome), (
                    [(outcome, 2)],
                    [f"Review of {self.gh.key} #3 comment 81 finished after its poller stopped; {outcome}"],
                    False, None))

    def test_review_that_outlived_its_poller_without_publishing_is_not_repeated(self):
        for starts in (1, 2):  # The poller is killed while the review publishes, and publishing fails.
            with self.subTest(starts=starts):
                self.assertEqual(self.outlived(starts, "failed"), (
                    [("failed", starts)],
                    [f"Review of {self.gh.key} #3 comment 81 finished after its poller stopped; "
                     "it did not publish, so marked failed, a new /review retries it"],
                    False, None))

    def test_the_outcome_the_review_child_records_is_the_one_the_poller_keeps(self):
        for report in ({"state": "reviewed", "publication": "posted-or-already-present"}, {"state": "reviewed"},
                       {"state": "model-unavailable", "publication": "posted-or-already-present"},
                       {"state": "partial", "publication": "failed"}):
            with self.subTest(report=report), patch("cursor_pr.poll.subprocess.run") as run:
                run.return_value.stdout = json.dumps(report).encode()
                self.assertEqual(run_review(self.gh, 3, self.state), cli.review_outcome(report))

    def test_failed_retry_is_reported(self):
        jobs = Jobs(self.state)
        jobs.enqueue(self.gh.key, 3, 81)
        jobs.next()
        cli.record_head(head_record(self.state), "a" * 40)
        jobs, _ = self.restart()
        with self.assertLogs("cursor_pr.poll", level="WARNING") as logged:
            self.run_cycle(jobs, answered=b"")  # The change has moved on: the child refuses and reports nothing.
        self.assertEqual([line.split("cursor_pr.poll:", 1)[1] for line in logged.output],
                         [f"Review of {self.gh.key} #3 comment 81 was not completed by its retry; "
                          "a new /review retries it"])
        self.assertEqual(self.sql("SELECT status, attempts FROM requests"), [("failed", 2)])
        self.assertIsNone(jobs.next())


class RecordedHeadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def review(self, *options):
        # The forge is replaced: the change is at head b whatever was asked for.
        change = Change("b" * 40, [], "https://github.com/acme/api/pull/3")
        args = cli.parser().parse_args(["--change", "acme/api#3", "--expected-change-url", change.identity,
                                        "--estimate", "--cache-dir", str(self.state / "cache"), *options])
        args.max_input_tokens = 6000
        with patch("cursor_pr.cli.forge_change", return_value=change):
            return cli.execute(args)

    def test_review_records_its_head_before_any_other_work(self):
        record = self.state / "requests.head"
        self.review("--record-head", str(record))
        self.assertEqual(record.read_text(), "\n" + "b" * 40 + "\n")
        record.write_text("acme/api 3 81\n\n")  # As the poller writes it for the request it started.
        self.review("--record-head", str(record))
        self.assertEqual(record.read_text(), "acme/api 3 81\n" + "b" * 40 + "\n")

    def test_repeat_refused_at_another_head_leaves_the_record_as_it_was(self):
        record = self.state / "requests.head"
        record.write_text(f"acme/api 3 81\n{'a' * 40}\n\n")
        with self.assertRaises(ReviewError):
            self.review("--record-head", str(record), "--expected-head", "a" * 40)
        self.assertEqual(record.read_text(), f"acme/api 3 81\n{'a' * 40}\n\n")

    def enter(self, *options, failing=None):
        # The review child's entry point, as the poller starts it, with the review itself replaced.
        project = dict(provider="github", url="https://github.com/acme/api", checkout=str(self.state),
                       requester_ids=[7])
        argv = ["cursor-review", "--change", "acme/api#3", "--expected-change-url",
                project_from(project).change_url(3), "--max-input-tokens", "6000", *options]
        with patch("sys.argv", argv), patch("cursor_pr.cli.run_request", side_effect=failing, return_value=(
                {"state": "reviewed", "publication": "posted-or-already-present"}, 0)) as request, \
                patch.dict(os.environ, {"CURSOR_REVIEW_POLLER_CHILD": "1", "REVIEW_STATE_DIR": str(self.state),
                                        "REVIEW_FORGE_PROJECT": json.dumps(project)}), \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            code = cli.main()
        return code, request, err.getvalue()

    def test_review_records_how_it_ended_before_its_report_can_fail(self):
        record = self.state / "requests.head"
        record.write_text(f"acme/api 3 81\n{'b' * 40}\n\n")

        class Gone:
            # The poller that would have read the report has been killed.
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self):
                raise BrokenPipeError(32, "Broken pipe")

        with patch("sys.stdout", Gone()):
            code, request, err = self.enter("--record-head", str(record))
        request.assert_called_once()
        self.assertEqual((code, "Broken pipe" in err), (2, True))
        self.assertEqual(record.read_text(), f"acme/api 3 81\n{'b' * 40}\nreviewed\n")  # Not a failed review.

    def test_review_that_fails_records_that(self):
        record = self.state / "requests.head"
        record.write_text(f"acme/api 3 81\n{'b' * 40}\n\n")
        code, request, err = self.enter("--record-head", str(record), failing=ReviewError("Forge is down"))
        request.assert_called_once()
        self.assertEqual((code, "Forge is down" in err), (2, True))
        self.assertEqual(record.read_text(), f"acme/api 3 81\n{'b' * 40}\nfailed\n")

    def test_repeat_at_the_same_head_is_reviewed(self):
        self.assertEqual(self.review("--expected-head", "b" * 40)[0]["state"], "estimate")

    def test_repeat_at_another_head_is_refused(self):
        with self.assertRaises(ReviewError) as refused:
            self.review("--expected-head", "a" * 40)
        self.assertEqual(str(refused.exception),
                         f"Change is at {'b' * 40}, not at {'a' * 40}, the head of its interrupted start")


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

    def test_review_child_holds_the_lock_until_it_exits(self):
        release = self.state / "release"
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
