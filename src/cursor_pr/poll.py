"""Poll configured forges and process explicit review requests serially."""
import argparse
import fcntl
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

from .cli import CLEANUP_SECONDS, QUEUED_REVIEW_SECONDS
from .config import load_config
from .credentials import SETUP_INPUTS, forge_credentials
from .discovery import poll_project
from .manager import ConfigurationWatch
from .login import auth_mode, ensure_session, login_directory
from . import acknowledge, buildinfo, history, setup, providers

log = logging.getLogger(__name__)

# How a restart settles a review it found running.
GIVEN_UP = "marked failed, a new /review retries it"
UNRECORDED = "marked failed, the head it started at was not recorded, a new /review retries it"

# How long the poller waits for a review child: the child's own limits and a margin.
REVIEW_WAIT_SECONDS = QUEUED_REVIEW_SECONDS + CLEANUP_SECONDS + 30

# The instance lock's descriptor while this process holds it. A review child inherits it, so the
# lock outlives a poller that is killed while a review runs.
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
            db.execute("""CREATE TABLE IF NOT EXISTS activations (
                project TEXT PRIMARY KEY, since TEXT NOT NULL)""")
            # Only the process holding the instance lock opens the queue, and a review child holds
            # that lock until it exits and settles its own row before it does. So a row still started
            # belongs to a review that was cut off. It runs once more, held to the head in its row if
            # it had resolved one. A request that keeps killing the poller must not loop.
            for *job, attempts in db.execute(
                    "SELECT project, number, comment, attempts FROM requests WHERE status='started'").fetchall():
                settled.append((job, "failed", GIVEN_UP) if attempts >= 2 else (job, "queued", "queued again"))
                db.execute("UPDATE requests SET status=? WHERE project=? AND number=? AND comment=?",
                           (settled[-1][1], *job))
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

    def enqueue(self, project, number, comment, status="queued"):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR IGNORE INTO requests (project, number, comment, status) VALUES (?,?,?,?)",
                       (project, number, comment, status))

    def next(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute("""UPDATE requests SET status='started', attempts=attempts+1 WHERE rowid=(
                SELECT rowid FROM requests WHERE status='queued' ORDER BY rowid LIMIT 1)
                RETURNING project, number, comment""").fetchone()

    def pending(self, project, number):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT 1 FROM requests WHERE project=? AND number=? AND status IN ('queued','started')",
                              (project, number)).fetchone() is not None

    def finish(self, job, status):
        """Settle a review the child did not settle itself, and return how the request ended."""
        with closing(sqlite3.connect(self.path)) as db, db:
            # The review child settles its own row. This is for a review that did not get that far.
            db.execute("""UPDATE requests SET status=? WHERE project=? AND number=? AND comment=?
                AND status='started'""", (status, *job))
            row = db.execute("SELECT status, attempts FROM requests WHERE project=? AND number=? AND comment=?",
                             job).fetchone()
        if row and row[0] == "failed" and row[1] > 1:
            # The head may have moved on since the first start, or the retry failed like the first did.
            log.warning("Review of %s #%d comment %d was not completed by its retry; a new /review retries it", *job)
        return row[0] if row else status


def run_review(project, number, state, comment=None):
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
    if os.getenv("REVIEW_MODEL"):
        argv.extend(["--model", os.environ["REVIEW_MODEL"]])
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
    # The review child inherits the instance lock, so the lock is held until it exits even when the
    # poller is killed first. No replacement recovers this request while the review can still run.
    # The child bounds itself, and its watchdog ends it and the commands it started. The wait here is
    # bounded past that as well, so that nothing left holding the report pipe can keep the queue waiting.
    try:
        result = subprocess.run(argv, cwd=project.checkout, env=env, stdout=subprocess.PIPE,
                                pass_fds=(lock_fd,) if lock_fd is not None else (), timeout=REVIEW_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        log.warning("Review %s did not end within %d seconds and was stopped", address, REVIEW_WAIT_SECONDS)
        return "failed"
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return "failed"
    log.info("Review %s state=%s publication=%s provider=%s model_invocations=%s", address, report["state"],
             report.get("publication"), report.get("provider", "cursor"), report.get("model_invocations"))
    if report.get("publication") == "failed":
        log.warning("Review %s publication failed: %r", address,
                    report.get("publication_error", "reason not reported"))
    return report["state"] if report.get("publication") == "posted-or-already-present" else "failed"


def cycle(projects, state, jobs, since, runner=None, ack=None):
    for project in projects.values():
        try:
            poll_project(project, jobs, since[project.key], ack)
        except Exception:
            log.exception("Poll failed for %s; will retry next cycle", project.key)
    while job := jobs.next():
        key, number, comment = job
        status = "failed"
        if key in projects:
            try:
                status = (runner(projects[key], number, state) if runner
                          else run_review(projects[key], number, state, comment))
            except Exception:
                log.exception("Review failed for %s #%d comment %d", key, number, comment)
        # The row, not the report, says how it ended: the child settles it before its report can fail.
        status = jobs.finish(job, status)
        if ack is not None and key in projects:
            ack.finished(projects[key], number, comment, status)


@contextmanager
def instance_lock(state):
    # A file of its own, not the directory: setup takes shared and exclusive locks on the
    # directory inode, and an exclusive lock there would block the setup wizard.
    global lock_fd
    fd = os.open(state / ".poller.lock", os.O_RDWR | os.O_CREAT, 0o600)
    held = lock_fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            setup.max_input_tokens(state)
            mine_seconds = history.interval()
            projects = load_config(state)
            stack.enter_context(providers.cursor_account())
            stack.enter_context(forge_credentials(projects))
        except (ValueError, KeyError, TypeError, EOFError, OSError, subprocess.TimeoutExpired) as err:
            parser.exit(2, f"Polling configuration: {err}\n")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        jobs = Jobs(state)
        ack = acknowledge.Acknowledgement(jobs, state)
        for key, number, comment in jobs.abandoned:
            if key in projects:
                ack.finished(projects[key], number, comment, "failed")
        since = {key: jobs.activated(key) for key in projects}
        log.info("Polling %s with %d seconds between cycles", ", ".join(projects), args.interval)
        if mine_seconds:
            threading.Thread(target=history.mine_periodically, args=(projects, state, mine_seconds),
                             name="qualgen-miner", daemon=True).start()
        while not watch.changed():
            if watch.enabled and any(p.managed and not p.checkout.is_dir() for p in projects.values()):
                watch.wait(2)
                continue
            cycle(projects, state, jobs, since, ack=ack)
            watch.wait(args.interval)


if __name__ == "__main__":
    main()
