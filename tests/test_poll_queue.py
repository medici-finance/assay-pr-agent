from contextlib import closing, nullcontext
from datetime import datetime, timezone
import io
import json
import os
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
START = 1_800_000_000.0


class Offline(unittest.TestCase):
    """Nothing here may reach a forge or a desk tool: an unmocked call fails instead of being made."""

    def setUp(self):
        def refuse(*args, **kwargs):
            raise AssertionError("This test tried to use the network or a desk tool")
        for name in ("socket.socket.connect", "cursor_pr.source.run_desk", "cursor_pr.acknowledge.run_desk"):
            self.start(patch(name, side_effect=refuse))

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()


def poller(state, *, cycle=None, env=()):
    """Run main() through one poll cycle with everything external replaced. Returns the stand-in for cycle."""
    (state / "projects.json").write_text("{}")
    watch = Mock(enabled=False)
    watch.changed.side_effect = [False, True]
    with patch("sys.argv", ["cursor-review-poll", "--state-dir", str(state)]), patch.dict(os.environ, dict(env)), \
         patch("cursor_pr.poll.ConfigurationWatch", return_value=watch), \
         patch("cursor_pr.poll.setup.apply_saved_setup"), \
         patch("cursor_pr.poll.providers.provider", return_value="cursor"), \
         patch("cursor_pr.poll.auth_mode", return_value="api-key"), \
         patch("cursor_pr.poll.setup.max_input_tokens", return_value=6000), \
         patch("cursor_pr.poll.history.interval", return_value=0), \
         patch("cursor_pr.poll.load_config", return_value={}), \
         patch("cursor_pr.poll.providers.cursor_account", side_effect=lambda: nullcontext()), \
         patch("cursor_pr.poll.forge_credentials", side_effect=lambda *args: nullcontext()), \
         patch("cursor_pr.poll.logging.basicConfig"), patch("cursor_pr.poll.cycle", side_effect=cycle) as run:
        main()
    return run


def comment(number, body="looks good to me"):
    created = f"2026-06-01T{number // 60:02d}:{number % 60:02d}:00Z"  # Later comments carry a later time.
    return dict(authorId=7, databaseId=number, authorType="User", body=body, createdAt=created, updatedAt=created)


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


class InstanceLockTests(Offline):
    def setUp(self):
        super().setUp()
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

        def cycle(projects, state, jobs, since, *args, **kwargs):
            self.assertIn("BlockingIOError", self.holder().stderr)
            opened.append(jobs.path)

        poller(self.state, cycle=cycle)
        self.assertEqual(opened, [self.state.resolve() / "requests.sqlite3"])
        self.assertEqual(self.holder().returncode, 0)
        self.assertEqual((self.state / ".poller.lock").stat().st_mode & 0o777, 0o600)


class ReadScheduleTests(Offline):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.gh = project_from(dict(provider="github", url="https://github.com/acme/github", requester_ids=[7],
                                    checkout=tmp.name))
        self.open, self.threads, self.partial, self.reads, self.reviews = [3], {3: [comment(81)]}, set(), [], []
        self.now = [START]
        self.start(patch("cursor_pr.discovery.time")).time.side_effect = lambda: self.now[0]
        self.start(patch("cursor_pr.discovery.run_desk", side_effect=self.desk))
        self.jobs = Jobs(self.state)
        self.since = {self.gh.key: datetime(2026, 1, 1, tzinfo=timezone.utc)}

    def desk(self, args, *, cwd, timeout, env=None):
        if args[0] == "deskmonitor":
            root = Path(env["PR_MONITOR_STATE_DIR"])
            root.mkdir(parents=True, exist_ok=True)
            (root / (args[2].replace("/", "__") + ".state")).write_text(
                "".join(f"{number} abc ready OPEN CLEAN\n" for number in self.open))
            return b""
        wanted = [int(arg.split("#")[1]) for arg in args[3::2]]
        self.reads.append(wanted)
        return json.dumps(dict(schema=1, kind="comments", partial=[
            dict(repo=self.gh.slug, number=number) for number in wanted if number in self.partial], items=[
            dict(repo=self.gh.slug, number=number, comments=self.threads[number]) for number in wanted
            if number not in self.partial])).encode()

    def tick(self, seconds=120):
        """Run one cycle `seconds` after the previous one; return the changes it asked to read."""
        self.now[0] += seconds
        self.reads.clear()
        cycle({self.gh.key: self.gh}, self.state, self.jobs, self.since,
              lambda project, number, state: self.reviews.append((self.now[0] - START, number)) or "reviewed",
              idle=(120, 900))
        return [number for wanted in self.reads for number in wanted]

    def schedule(self):
        with closing(sqlite3.connect(self.state / "requests.sqlite3")) as db:
            return db.execute("SELECT number, comments, wait, seen - ? FROM schedule ORDER BY number", (START,)).fetchall()

    def test_quiet_change_is_read_less_often_and_never_waits_more_than_the_cap(self):
        self.assertEqual(self.tick(0), [3])  # A change seen for the first time is read at once.
        read_at = [0]
        for _ in range(30):
            if self.tick():
                read_at.append(self.now[0] - START)
        self.assertEqual(read_at, [0, 120, 360, 840, 1800, 2760])  # 120, 240, 480, then 900 seconds.
        self.assertLessEqual(max(b - a for a, b in zip(read_at, read_at[1:])), 900 + 120)
        self.assertEqual(self.schedule(), [(3, 1, 900, 2760)])

    def test_new_comments_return_the_change_to_the_poll_interval(self):
        self.tick(0)
        for _ in range(3):
            self.tick()  # Read at 0, 120 and 360; the wait is now 480.
        self.assertEqual(self.tick(), [])
        self.threads[3].append(comment(82))
        self.assertEqual(self.tick(), [])  # 600: nothing says this change changed, so it waits until 840.
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(), [3])
        self.assertEqual(self.schedule(), [(3, 2, 120, 840)])
        self.assertEqual(self.tick(), [3])
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(), [3])

    def test_request_on_a_quiet_change_is_found_at_its_next_due_read(self):
        self.tick(0)
        for _ in range(7):
            self.tick()  # Read at 0, 120, 360 and 840; the next is due at 1740.
        self.threads[3].append(comment(90, "/review") | dict(url=self.gh.change_url(3) + "#issuecomment-90"))
        posted = self.now[0] - START
        for _ in range(9):
            self.tick()
        self.assertEqual(self.reviews, [(1800, 3)])
        self.assertLessEqual(self.reviews[0][0] - posted, 900 + 120)

    def test_restart_keeps_the_schedule(self):
        self.tick(0)
        self.tick()  # Read at 0 and 120; the next read is due at 360.
        self.jobs = Jobs(self.state)  # A new poller process on the same state volume.
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(), [3])

    def test_partial_read_does_not_advance_the_schedule(self):
        self.open, self.threads = [3, 4], {3: [comment(81)], 4: [comment(82)]}
        self.assertEqual(self.tick(0), [3, 4])
        self.partial = {4}
        with self.assertLogs("cursor_pr.discovery", level="WARNING"):
            self.assertEqual(self.tick(), [3, 4])  # Asked for again at 120; only 3 comes back.
        self.threads[4].append(comment(90, "/review") | dict(url=self.gh.change_url(4) + "#issuecomment-90"))
        self.partial = set()
        self.assertEqual(self.tick(), [3, 4])  # Both are due at 240, so the partial read advanced nothing.
        self.assertEqual(self.reviews, [(240, 4)])
        self.open.append(5)
        self.threads[5] = []
        self.partial = {5}
        with self.assertLogs("cursor_pr.discovery", level="WARNING"):
            self.assertEqual(self.tick(), [4, 5])  # 360: 4 is back at the poll interval since it had a request;
            #                                      5 was never read completely, so it is asked for every cycle.
        with self.assertLogs("cursor_pr.discovery", level="WARNING"):
            self.assertEqual(self.tick(), [3, 4, 5])  # 480
        self.assertEqual(self.tick(), [3, 4, 5])  # 600: an incomplete batch holds back each change in it.
        self.assertEqual([row[0] for row in self.schedule()], [3, 4])
        self.partial, self.open = set(), [4, 5]
        self.assertEqual(self.tick(), [4, 5])  # 720
        self.assertEqual([row[0] for row in self.schedule()], [4, 5])  # A closed change is forgotten.

    def test_cycle_logs_how_many_changes_it_read_and_how_many_it_skipped(self):
        self.open = [3, 4, 5]
        self.threads = {number: [comment(number)] for number in self.open}
        self.assertEqual(self.tick(0), [3, 4, 5])
        with self.assertLogs("cursor_pr.poll", level="INFO") as logged:
            self.tick()
        self.assertEqual(logged.output, ["INFO:cursor_pr.poll:Read the comments of 3 open changes; "
                                         "0 quiet ones are due in a later cycle"])
        with self.assertLogs("cursor_pr.poll", level="INFO") as logged:
            self.assertEqual(self.tick(), [])
        self.assertEqual(logged.output, ["INFO:cursor_pr.poll:Read the comments of 0 open changes; "
                                         "3 quiet ones are due in a later cycle"])
        self.assertEqual(self.schedule()[-1][2:], (240, 120))

    def test_setting_is_validated_at_startup_and_disables_itself_at_the_poll_interval(self):
        for value, idle in (("900", (120, 900)), ("120", None), ("60", None)):
            with self.subTest(value=value):
                self.assertEqual(poller(self.state, env={"REVIEW_POLL_SECONDS": "120", "REVIEW_POLL_IDLE_MAX_SECONDS": value})
                                 .call_args.kwargs["idle"], idle)
        for value in ("0", "-5", "soon"):
            with self.subTest(value=value), patch.dict(os.environ, {"REVIEW_POLL_IDLE_MAX_SECONDS": value}), \
                 patch("sys.stderr", new_callable=io.StringIO) as err, self.assertRaises(SystemExit) as stopped:
                poller(self.state, env={"REVIEW_POLL_SECONDS": "120"})
            self.assertEqual(stopped.exception.code, 2)
            self.assertTrue(err.getvalue().startswith("Polling configuration: "))


if __name__ == "__main__":
    unittest.main()
