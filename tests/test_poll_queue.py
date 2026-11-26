from contextlib import closing, nullcontext
import io
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from cursor_pr.config import project_from
from cursor_pr.poll import Jobs, cycle, instance_lock, main

HOLDER = """from pathlib import Path
import sys
from cursor_pr.poll import instance_lock
with instance_lock(Path(sys.argv[1])):
    pass
"""


class QueueRestartTests(unittest.TestCase):
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

    def test_queue_from_the_old_schema_migrates(self):
        with closing(sqlite3.connect(self.state / "requests.sqlite3")) as db, db:
            db.execute("""CREATE TABLE requests (project TEXT NOT NULL, number INTEGER NOT NULL,
                comment INTEGER NOT NULL, status TEXT NOT NULL, PRIMARY KEY(project, number, comment))""")
            db.executemany("INSERT INTO requests VALUES (?,?,?,?)", [(self.gh.key, 3, 81, "queued"),
                                                                    (self.gl.key, 4, 82, "started")])
        jobs, logged = self.restart()
        self.assertEqual(logged, [f"Review of {self.gl.key} #4 comment 82 was interrupted by a restart; queued again"])
        self.assertEqual([row[1] for row in self.sql("PRAGMA table_info(requests)")],
                         ["project", "number", "comment", "status", "attempts"])
        self.assertEqual(self.sql("SELECT number, status, attempts FROM requests"), [(3, "queued", 0), (4, "queued", 1)])
        jobs.enqueue(self.gh.key, 5, 83)
        self.assertEqual(jobs.next(), (self.gh.key, 3, 81))
        self.assertEqual(jobs.next(), (self.gl.key, 4, 82))
        self.assertEqual(jobs.next(), (self.gh.key, 5, 83))
        self.assertEqual(self.sql("SELECT attempts FROM requests ORDER BY rowid"), [(1,), (2,), (1,)])
        # All three die with the poller now: the migrated row in its retry, the others in a first start.
        jobs, logged = self.restart()
        self.assertEqual(sorted(line.rsplit("; ", 1)[1] for line in logged),
                         ["marked failed, a new /review retries it", "queued again", "queued again"])
        self.assertEqual(self.sql("SELECT number, status FROM requests ORDER BY rowid"),
                         [(3, "queued"), (4, "failed"), (5, "queued")])


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
