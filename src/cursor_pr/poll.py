"""Poll configured forges and process explicit review requests serially."""
import argparse
import fcntl
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import threading
import time

from .config import FINDINGS_SETTINGS, load_config
from .credentials import SETUP_INPUTS, forge_credentials
from .discovery import poll_project
from .manager import ConfigurationWatch
from .login import auth_mode, ensure_session, login_directory
from . import acknowledge, buildinfo, history, setup, providers
from .acknowledge import PENDING as MARK_PENDING

log = logging.getLogger(__name__)

# How a restart settles a review it found running.
GIVEN_UP = "marked failed, a new /review retries it"
UNRECORDED = "marked failed, the head it started at was not recorded, a new /review retries it"

# The longest a review may run. Its waits on the forge, the model and the scanners have timeouts of
# their own. This is for one that hangs anyway.
REVIEW_SECONDS = 1800
# How long the supervisor is given to end a review before the poller stops waiting for it.
STOP_SECONDS = 30
# How long what a review printed is still read for once its supervisor has ended.
DRAIN_SECONDS = 2
# How long a poller waits for the lock at its start. A poller that was just stopped leaves the lock
# with its review's supervisor for the few seconds that takes to end the review.
LOCK_WAIT_SECONDS = 15
SUPERVISOR = Path(__file__).with_name("supervise.py")

# The instance lock's descriptor while this process holds it. A review's supervisor and the review
# inherit it, so the lock is not free again until a review that outlived its poller has been ended.
lock_fd = None


class Jobs:
    """The request queue. A request's row is all that is kept about it: its status, how often it was
    started, and the head its review resolved. The review child writes the head and the final status
    to the row itself, each in one transaction, so no restart finds them disagreeing."""

    def __init__(self, state):
        self.path = state / "requests.sqlite3"
        settled = []
        with closing(sqlite3.connect(self.path)) as db, db:
            # One transaction for the schema and the recovery: sqlite3 opens none for DDL by itself,
            # and a poller that stops halfway must leave nothing behind.
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                project TEXT NOT NULL, number INTEGER NOT NULL, comment INTEGER NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, head TEXT NOT NULL DEFAULT '',
                prepared TEXT,
                PRIMARY KEY(project, number, comment))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
            if "attempts" not in columns:
                db.execute("ALTER TABLE requests ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            if "head" not in columns:
                db.execute("ALTER TABLE requests ADD COLUMN head TEXT NOT NULL DEFAULT ''")
                # A version that kept no head left these running. They may have published at a head
                # nobody recorded, so they cannot be repeated safely.
                settled += [(row, "failed", UNRECORDED) for row in db.execute(
                    "SELECT project, number, comment FROM requests WHERE status='started'").fetchall()]
                db.execute("UPDATE requests SET status='failed' WHERE status='started'")
            if "prepared" not in columns:
                db.execute("ALTER TABLE requests ADD COLUMN prepared TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS activations (
                project TEXT PRIMARY KEY, since TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS schedule (
                project TEXT NOT NULL, number INTEGER NOT NULL, comments INTEGER NOT NULL, newest TEXT NOT NULL,
                wait INTEGER NOT NULL, seen REAL NOT NULL, PRIMARY KEY(project, number))""")
            db.execute("""CREATE TABLE IF NOT EXISTS acknowledgements (
                project TEXT NOT NULL, number INTEGER NOT NULL, comment INTEGER NOT NULL, marker_id INTEGER,
                notice_sent INTEGER NOT NULL DEFAULT 0, tries INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, cleared INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(project, number, comment))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(acknowledgements)")}
            for column, kind in (("tries", "INTEGER"), ("retry_at", "REAL"), ("cleared", "INTEGER")):
                if column not in columns:
                    db.execute(f"ALTER TABLE acknowledgements ADD COLUMN {column} {kind} NOT NULL DEFAULT 0")
            # Only the process holding the instance lock opens the queue, and a review child holds
            # that lock until it exits and settles its own row before it does. So a row still started
            # belongs to a review that was cut off. It runs once more, held to the head in its row if
            # it had resolved one. A request that keeps killing the poller must not loop.
            for *job, attempts in db.execute(
                    "SELECT project, number, comment, attempts FROM requests WHERE status='started'").fetchall():
                settled.append((job, "failed", GIVEN_UP) if attempts >= 2 else (job, "queued", "queued again"))
                db.execute("""UPDATE requests SET status=?, prepared=CASE WHEN ?='failed' THEN NULL ELSE prepared END
                    WHERE project=? AND number=? AND comment=?""", (settled[-1][1], settled[-1][1], *job))
            db.executemany("INSERT OR IGNORE INTO acknowledgements (project, number, comment) VALUES (?,?,?)",
                           [job for job, status, _ in settled if status == "failed"])
        for job, _, reason in settled:
            log.warning("Review of %s #%d comment %d was interrupted by a restart; %s", *job, reason)
        # Requests this start gave up on, so the requester can be told.
        self.abandoned = [tuple(job) for job, status, _ in settled if status == "failed"]

    def activated(self, project):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR IGNORE INTO activations VALUES (?,?)",
                       (project, datetime.now(timezone.utc).isoformat()))
            return datetime.fromisoformat(db.execute(
                "SELECT since FROM activations WHERE project=?", (project,)).fetchone()[0])

    def recorded(self, project, number, comment):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT 1 FROM requests WHERE project=? AND number=? AND comment=?",
                              (project, number, comment)).fetchone() is not None

    def enqueue(self, project, number, comment, status="queued", *, acknowledge=False):
        with closing(sqlite3.connect(self.path)) as db, db:
            inserted = db.execute("INSERT OR IGNORE INTO requests (project, number, comment, status) VALUES (?,?,?,?)",
                                  (project, number, comment, status))
            if acknowledge and inserted.rowcount:
                db.execute("INSERT INTO acknowledgements (project, number, comment, marker_id) VALUES (?,?,?,?)",
                           (project, number, comment, MARK_PENDING))

    def next(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute("""UPDATE requests SET status='started', attempts=attempts+1 WHERE rowid=(
                SELECT rowid FROM requests WHERE status='queued' ORDER BY rowid LIMIT 1)
                RETURNING project, number, comment""").fetchone()

    def pending(self, project, number):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT 1 FROM requests WHERE project=? AND number=? AND status IN ('queued','started')",
                              (project, number)).fetchone() is not None

    def waiting(self, project, numbers, now, cap, listing_complete):
        """Open changes whose saved read is not due yet. A change no longer open is forgotten."""
        numbers = set(numbers)
        with closing(sqlite3.connect(self.path)) as db, db:
            saved = {number: (wait, seen) for number, wait, seen in db.execute(
                "SELECT number, wait, seen FROM schedule WHERE project=?", (project,))}
            if listing_complete:
                db.executemany("DELETE FROM schedule WHERE project=? AND number=?",
                               [(project, number) for number in saved if number not in numbers])
        # A clock that moved back past the last read makes the saved times meaningless, so read now.
        return {number for number, (wait, seen) in saved.items()
                if number in numbers and now >= seen and seen + min(wait, cap) > now}

    def observed(self, project, number, comments, newest, active, now, interval, cap):
        """Keep one complete read of a change. A first look, new comments or a request read again after one
        interval; an unchanged conversation waits twice as long as before, never longer than the cap."""
        with closing(sqlite3.connect(self.path)) as db, db:
            previous = db.execute("SELECT comments, newest, wait FROM schedule WHERE project=? AND number=?",
                                  (project, number)).fetchone()
            quiet = previous is not None and not active and list(previous[:2]) == [comments, newest]
            db.execute("INSERT OR REPLACE INTO schedule VALUES (?,?,?,?,?,?)",
                       (project, number, comments, newest, min(previous[2] * 2, cap) if quiet else interval, now))

    def unfinished_acknowledgements(self, now=None):
        """Finished requests that still owe the forge something; with a time, only those due a try by then."""
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("""SELECT project, number, comment, status FROM requests JOIN acknowledgements
                USING (project, number, comment) WHERE status NOT IN ('queued','started')
                AND (?1 IS NULL OR retry_at <= ?1)""", (now,)).fetchall()

    def acknowledgement_tries(self, job):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT tries FROM acknowledgements WHERE project=? AND number=? AND comment=?", job).fetchone()
            return row[0] if row else 0

    def retry_acknowledgement(self, job, at):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""UPDATE acknowledgements SET tries=tries+1, retry_at=?
                WHERE project=? AND number=? AND comment=?""", (at, *job))

    def unmarked(self):
        """Waiting requests whose marker was never asked for: the poller stopped right after queueing them."""
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("""SELECT project, number, comment FROM requests JOIN acknowledgements
                USING (project, number, comment) WHERE status='queued' AND marker_id=?""",
                              (MARK_PENDING,)).fetchall()

    def acknowledgement(self, job):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("""SELECT marker_id, notice_sent, cleared FROM acknowledgements
                WHERE project=? AND number=? AND comment=?""", job).fetchone()

    def record_marker(self, job, marker):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE acknowledgements SET marker_id=? WHERE project=? AND number=? AND comment=?",
                       (marker, *job))

    def record_notice(self, job):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE acknowledgements SET notice_sent=1 WHERE project=? AND number=? AND comment=?", job)

    def record_cleared(self, job):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE acknowledgements SET cleared=1 WHERE project=? AND number=? AND comment=?", job)

    def acknowledge_pending(self, project, number):
        """Give every waiting request on the change a record, so that the last to finish clears its marker."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""INSERT OR IGNORE INTO acknowledgements (project, number, comment)
                SELECT project, number, comment FROM requests
                WHERE project=? AND number=? AND status IN ('queued','started')""", (project, number))

    def clear_acknowledgement(self, job):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM acknowledgements WHERE project=? AND number=? AND comment=?", job)

    def finish(self, job, status):
        """Settle a review the child did not settle itself, and return how the request ended."""
        with closing(sqlite3.connect(self.path)) as db, db:
            # The review child settles its own row. This is for a review that did not get that far.
            db.execute("""UPDATE requests SET status=?, prepared=NULL WHERE project=? AND number=? AND comment=?
                AND status='started'""", (status, *job))
            row = db.execute("SELECT status, attempts FROM requests WHERE project=? AND number=? AND comment=?",
                             job).fetchone()
        if row and row[0] == "failed" and row[1] > 1:
            # The head may have moved on since the first start, or the retry failed like the first did.
            log.warning("Review of %s #%d comment %d was not completed by its retry; a new /review retries it", *job)
        return row[0] if row else status

    # Tiers live in their own table: a request without a row is a plain /review.
    TIERS = "CREATE TABLE IF NOT EXISTS tiers (project TEXT, number INTEGER, comment INTEGER, tier TEXT NOT NULL, " \
            "PRIMARY KEY(project, number, comment))"

    def set_tier(self, project, number, comment, tier):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(self.TIERS)
            db.execute("INSERT OR IGNORE INTO tiers VALUES (?,?,?,?)", (project, number, comment, tier))

    def tier(self, job):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(self.TIERS)
            row = db.execute("SELECT tier FROM tiers WHERE project=? AND number=? AND comment=?", job).fetchone()
            return row[0] if row else "regular"


def run_review(project, number, state, tier="regular", comment=None):
    address = f"{project.slug}#{number}"
    argv = [sys.executable, "-I", "-m", "cursor_pr.cli", "--repo", str(project.checkout),
            "--change", address, "--expected-change-url", project.change_url(number),
            "--post", "--cache-dir", str(state / "cache")]
    if history.interval():
        argv.extend(["--qualgen-store", str(history.store_for(state, project))])
    if project.scanner_ci:
        argv.extend(["--scanner-ci", json.dumps(project.scanner_ci)])
    if project.go_analysis:
        argv.extend(["--go-analysis-producer", project.go_analysis])
    if os.getenv("REVIEW_MAX_INPUT_TOKENS"):
        argv.extend(["--max-input-tokens", os.environ["REVIEW_MAX_INPUT_TOKENS"]])
    if os.getenv(FINDINGS_SETTINGS[tier]):
        argv.extend(["--max-findings", os.environ[FINDINGS_SETTINGS[tier]]])
    if os.getenv("REVIEW_MODEL"):
        argv.extend(["--model", os.environ["REVIEW_MODEL"]])
    if tier != "regular":
        argv.extend(["--tier", tier])
    if comment is not None:
        # The child keeps the request's row itself: the head it resolves, which a repeat of the
        # request is held to, and how it ended, for a poller that stops before it can read the report.
        argv.extend(["--queue", str(state / "requests.sqlite3"), "--request", project.key, str(number), str(comment)])
    env = dict(os.environ)
    env["CURSOR_REVIEW_POLLER_CHILD"] = "1"
    env["REVIEW_STATE_DIR"] = str(state)
    env["REVIEW_FORGE_PROJECT"] = json.dumps(dict(provider=project.provider, url=project.url,
        managed=project.managed, checkout=str(project.checkout), requester_ids=sorted(project.requesters),
        visibility=project.visibility, gitlab_base=project.url.removesuffix("/" + project.slug)))
    for key in ("REVIEW_PROJECTS", *SETUP_INPUTS):
        env.pop(key, None)
    for key in list(env):
        if key.startswith("REVIEW_SECRET_"):
            del env[key]
    stdout = supervised(argv, cwd=project.checkout, env=env)
    if stdout is None:
        log.warning("Review %s did not end within %d seconds and was stopped", address, REVIEW_SECONDS + STOP_SECONDS)
        return "failed"
    try:
        report = json.loads(stdout)
    except ValueError:
        return "failed"
    log.info("Review %s state=%s publication=%s provider=%s model_invocations=%s", address, report["state"],
             report.get("publication"), report.get("provider", "cursor"), report.get("model_invocations"))
    if report.get("publication") == "failed":
        log.warning("Review %s publication failed: %r", address,
                    report.get("publication_error", "reason not reported"))
    return report["state"] if report.get("publication") == "posted-or-already-present" else "failed"


def supervised(argv, **options):
    """Run a review under its supervisor and return what it printed, or None if it had to be given up on.

    The supervisor ends the review, and whatever the review started, when this process is gone, when
    the review's time is up, or when the review ends. This process tells it that it is gone by
    nothing more than exiting: the supervisor holds the read end of a pipe that only this process
    can write to. So a review never runs beside a later poller, however this one stops.

    The supervisor, the review and all the review starts are one process group, which has the
    supervisor's number. If the supervisor is stuck or was killed, this process ends that group.
    """
    lifeline, hold = os.pipe()
    # The supervisor holds the other end of this one for as long as it runs, and gives it to nobody.
    # It says that the supervisor has ended without the poller having to wait for it, which would
    # give up the supervisor's number while the group, which has the same number, is still needed.
    over, running = os.pipe()
    keep = (lock_fd,) if lock_fd is not None else ()
    process = None
    try:
        # Without the poller's input: the group is not the terminal's foreground one, and a command
        # that read from the terminal there would be stopped by it and hang.
        process = subprocess.Popen(
            [sys.executable, "-I", str(SUPERVISOR), str(lifeline), str(REVIEW_SECONDS),
             ",".join(map(str, keep)), *argv], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            pass_fds=(lifeline, running, *keep), process_group=0, **options)
        os.close(running)
        running = None
        return read_report(process.stdout.fileno(), over, REVIEW_SECONDS + STOP_SECONDS)
    finally:
        # Closing the poller's end is what tells the supervisor to end the review: on a timeout,
        # on an interrupt, and on any other way out of here.
        os.close(lifeline)
        os.close(hold)
        if running is not None:
            os.close(running)
        if process is not None:
            ended = read_all(over, STOP_SECONDS) is not None
            # Neither of these waits for the supervisor, so its number, which is the group's, is
            # still its own when the group is ended. The supervisor is stopped first, so that it
            # cannot start anything more meanwhile.
            for end in ((os.kill,) if not ended else ()) + (os.killpg,):
                try:
                    end(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            process.wait()
            process.stdout.close()
        os.close(over)


def read_report(report, over, seconds):
    """What the review printed, or None when neither it nor its supervisor ended in time.

    The report is complete when its pipe closes. If the supervisor ends first, nothing more is
    coming from a review it ended, so what has been written by then is all there is: a command
    that left the group and kept the pipe open does not keep the queue waiting.
    """
    parts = []
    with selectors.DefaultSelector() as waiting:
        waiting.register(report, selectors.EVENT_READ)
        waiting.register(over, selectors.EVENT_READ)
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            readable = {key.fd for key, _ in waiting.select(left)}
            if over in readable:
                # The supervisor has ended. What is still in the pipe is taken, for a short time
                # only, whether or not something goes on writing to it.
                waiting.unregister(over)
                deadline = min(deadline, time.monotonic() + DRAIN_SECONDS)
            if report in readable:
                part = os.read(report, 65536)
                if not part:
                    return b"".join(parts)
                parts.append(part)
        return b"".join(parts) if over not in waiting.get_map() else None


def read_all(descriptor, seconds):
    """Everything written to the descriptor until it is closed, or None when that takes too long."""
    parts = []
    with selectors.DefaultSelector() as waiting:
        waiting.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            if waiting.select(left):
                part = os.read(descriptor, 65536)
                if not part:
                    return b"".join(parts)
                parts.append(part)
    return None


def cycle(projects, state, jobs, since, runner=None, ack=None, idle=None):
    if ack is not None:
        ack.settle(projects)
    polled = [0, 0]
    for project in projects.values():
        try:
            counts = poll_project(project, jobs, since[project.key], ack, idle)
            if idle is not None:
                polled = [total + count for total, count in zip(polled, counts)]
        except Exception:
            log.exception("Poll failed for %s; will retry next cycle", project.key)
    if idle is not None:
        log.info("Read the comments of %d open changes; %d quiet ones are due in a later cycle", *polled)
    if ack is not None:
        ack.mark(projects)
    while job := jobs.next():
        key, number, comment = job
        status = "failed"
        if key in projects:
            try:
                tier = jobs.tier(job)
                extra = () if tier == "regular" else (tier,)
                status = (runner(projects[key], number, state, *extra) if runner
                          else run_review(projects[key], number, state, tier, comment))
            except Exception:
                log.exception("Review failed for %s #%d comment %d", key, number, comment)
        # The row, not the report, says how it ended: the child settles it before its report can fail.
        status = jobs.finish(job, status)
        if ack is not None and key in projects:
            ack.finished(projects[key], number, comment, status)


@contextmanager
def instance_lock(state, wait=None):
    # A file of its own, not the directory: setup takes shared and exclusive locks on the
    # directory inode, and an exclusive lock there would block the setup wizard.
    global lock_fd
    fd = os.open(state / ".poller.lock", os.O_RDWR | os.O_CREAT, 0o600)
    held = lock_fd
    try:
        deadline = time.monotonic() + (LOCK_WAIT_SECONDS if wait is None else wait)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        lock_fd = fd
        yield
    finally:
        lock_fd = held
        os.close(fd)


def container_main():
    print(buildinfo.banner(), flush=True)
    if sys.argv[1:2] == ["manage"]:
        from .manager import console
        return console()
    if sys.argv[1:2] == ["setup"]:
        return setup.main(sys.argv[2:])
    if sys.argv[1:2] == ["login"]:
        return setup.login_main(sys.argv[2:])
    if sys.argv[1:2] == ["sync"]:
        from .sync import main as sync
        return sync()
    return main()


def main():
    parser = argparse.ArgumentParser(description="Poll GitHub and GitLab for /review comments")
    parser.add_argument("--state-dir", type=Path, default=setup.state_dir())
    parser.add_argument("--interval", type=int,
                        help="seconds to wait after each cycle (default 120)")
    args = parser.parse_args()
    state = args.state_dir.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with ExitStack() as stack:
        try:
            stack.enter_context(instance_lock(state))
        except BlockingIOError:
            parser.exit(2, "Another poller is already running on this state volume.\n")
        except OSError as err:
            # No lock, no poller: without it a second one would repeat the first one's running reviews.
            parser.exit(2, f"Polling lock: {err}\n")
        watch = ConfigurationWatch(state)
        try:
            fresh = not any((state / p).exists() for p in ("setup", "projects.json", "hooks.json"))
            first_install = (fresh and sys.stdin.isatty() and "REVIEW_PROJECTS" not in os.environ
                    and not any(k in os.environ for k in SETUP_INPUTS))
            if first_install:
                setup.configure(state)
            setup.apply_saved_setup(state)
            backend = providers.provider()
            if auth_mode(backend) == "login":
                directory = login_directory(state)
                if os.getenv("REVIEW_LOGIN_PROFILE"):
                    stack.enter_context(setup.locked(directory, fcntl.LOCK_SH))
                ensure_session(backend, directory, interactive=first_install)
            if args.interval is None:
                args.interval = int(os.getenv("REVIEW_POLL_SECONDS", "120"))
            if args.interval < 1:
                raise ValueError("poll interval must be positive")
            idle_max = int(os.getenv("REVIEW_POLL_IDLE_MAX_SECONDS", "900"))
            if idle_max < 1:
                raise ValueError("REVIEW_POLL_IDLE_MAX_SECONDS must be positive")
            setup.max_input_tokens(state)
            setup.max_findings(state)
            mine_seconds = history.interval()
            projects = load_config(state)
            stack.enter_context(providers.cursor_account())
            stack.enter_context(forge_credentials(projects))
        except (ValueError, KeyError, TypeError, EOFError, OSError, subprocess.TimeoutExpired) as err:
            parser.exit(2, f"Polling configuration: {err}\n")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        jobs = Jobs(state)
        ack = acknowledge.Acknowledgement(jobs, state)
        since = {key: jobs.activated(key) for key in projects}
        log.info("Polling %s with %d seconds between cycles", ", ".join(projects), args.interval)
        # A cap no longer than the poll interval reads every change on every cycle, as before the schedule.
        idle = (args.interval, idle_max) if idle_max > args.interval else None
        if idle:
            log.info("A quiet change can wait up to %d seconds before it is due for another read", idle_max)
        if mine_seconds:
            threading.Thread(target=history.mine_periodically, args=(projects, state, mine_seconds),
                             name="qualgen-miner", daemon=True).start()
        while not watch.changed():
            if watch.enabled and any(p.managed and not p.checkout.is_dir() for p in projects.values()):
                watch.wait(2)
                continue
            cycle(projects, state, jobs, since, ack=ack, idle=idle)
            watch.wait(args.interval)


if __name__ == "__main__":
    main()
