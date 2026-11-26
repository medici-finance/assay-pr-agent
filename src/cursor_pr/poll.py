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
import tempfile
import threading

from .config import load_config
from .credentials import SETUP_INPUTS, forge_credentials
from .discovery import poll_project
from .manager import ConfigurationWatch
from .login import auth_mode, ensure_session, login_directory
from . import buildinfo, history, setup, providers

log = logging.getLogger(__name__)

# How a restart settles a review it found running.
GIVEN_UP = "marked failed, a new /review retries it"
UNRECORDED = "marked failed, the head it started at was not recorded, a new /review retries it"

# The instance lock's descriptor while this process holds it. A review child inherits it, so the
# lock outlives a poller that is killed while a review runs.
lock_fd = None


def head_record(state):
    # One request runs at a time, so one file holds what is known about the running one: the
    # request's identity, which the poller writes before it starts the request; under it the head its
    # review resolved, which the review child writes before its model call; and under that how the
    # review ended, which the child writes before it exits, for a poller that stops before it can
    # read the report.
    return state / "requests.head"


def read_head_record(state):
    # The request that owns the record, the head recorded for it and how its review ended once it
    # did; no request, no head and no outcome when there is no readable record.
    try:
        request, head, outcome, _ = (head_record(state).read_text().split("\n") + ["", ""])[:4]
        key, number, comment = request.split(" ")
        return (key, int(number), int(comment)), head.strip(), outcome.strip()
    except (OSError, ValueError):
        return None, "", ""


def write_head_record(state, job, head=""):
    # Replaced whole, so a poller that stops meanwhile never finds part of a record.
    with tempfile.NamedTemporaryFile("w", encoding="ascii", dir=state, delete=False) as tmp:
        tmp.write(f"{job[0]} {job[1]} {job[2]}\n{head}\n")
    os.replace(tmp.name, head_record(state))


class Jobs:
    def __init__(self, state):
        self.path = state / "requests.sqlite3"
        self.state = state
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                project TEXT NOT NULL, number INTEGER NOT NULL, comment INTEGER NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(project, number, comment))""")
            if "attempts" not in {row[1] for row in db.execute("PRAGMA table_info(requests)")}:
                db.execute("ALTER TABLE requests ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            db.execute("""CREATE TABLE IF NOT EXISTS activations (
                project TEXT PRIMARY KEY, since TEXT NOT NULL)""")
            # Only the process holding the instance lock opens the queue, so a row still started was
            # left by a review whose poller stopped. The review child holds the lock until it exits,
            # so the review either ended, and its record says how, or was cut off with the poller. An
            # ended one is kept as it came out. A cut-off one runs once more, at the head it had
            # resolved: a request that keeps killing the poller must not loop, and one whose head is
            # not recorded, as from a version before the record, cannot be run safely.
            recorded, _, outcome = read_head_record(state)
            for key, number, comment, attempts in db.execute(
                    "SELECT project, number, comment, attempts FROM requests WHERE status='started'").fetchall():
                if recorded != (key, number, comment):
                    status, reason = "failed", "was interrupted by a restart; " + UNRECORDED
                elif outcome:
                    status, reason = outcome, "finished after its poller stopped; " + (
                        "it did not publish, so marked failed, a new /review retries it" if outcome == "failed"
                        else outcome)
                elif attempts >= 2:
                    status, reason = "failed", "was interrupted by a restart; " + GIVEN_UP
                else:
                    status, reason = "queued", "was interrupted by a restart; queued again"
                db.execute("UPDATE requests SET status=? WHERE project=? AND number=? AND comment=?",
                           (status, key, number, comment))
                log.warning("Review of %s #%d comment %d %s", key, number, comment, reason)
        if outcome:
            # The review that wrote it has ended, so its request is not to be run again at its head.
            head_record(state).unlink(missing_ok=True)

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
            row = db.execute("""SELECT rowid, project, number, comment FROM requests
                WHERE status='queued' ORDER BY rowid LIMIT 1""").fetchone()
            if row is None:
                return None
            job = (row[1], row[2], row[3])
            # Claimed for the request before the row says it is started, so a poller that stops in
            # between never leaves a start that comes back to another request's head. A request queued
            # again keeps the head its first start recorded, unless that review has already ended.
            request, _, outcome = read_head_record(self.state)
            if request != job or outcome:
                write_head_record(self.state, job)
            db.execute("UPDATE requests SET status='started', attempts=attempts+1 WHERE rowid=?", (row[0],))
            return job

    def finish(self, job, status):
        with closing(sqlite3.connect(self.path)) as db, db:
            attempts = db.execute("""UPDATE requests SET status=? WHERE project=? AND number=? AND comment=?
                RETURNING attempts""", (status, *job)).fetchone()
        head_record(self.state).unlink(missing_ok=True)
        if attempts and attempts[0] > 1 and status == "failed":
            # The head may have moved on since the first start, or the retry failed like the first did.
            log.warning("Review of %s #%d comment %d was not completed by its retry; a new /review retries it", *job)


def run_review(project, number, state):
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
    # The record names this request and, once a review of it has resolved a head, the head this run
    # must stay at. Until then this run is the first to get that far and says which head it reviews.
    # The child adds how it ended, for a poller that stops before it can read the report.
    argv.extend(["--record-head", str(head_record(state))])
    if head := read_head_record(state)[1]:
        argv.extend(["--expected-head", head])
    env = dict(os.environ)
    env["CURSOR_REVIEW_POLLER_CHILD"] = "1"
    env.pop("REVIEW_MANAGED_PROJECT", None)
    if project.managed:
        env["REVIEW_MANAGED_PROJECT"] = json.dumps(dict(provider=project.provider, url=project.url,
                                                       managed=True, requester_ids=sorted(project.requesters),
                                                       gitlab_base=project.url.removesuffix("/" + project.slug)))
    for key in ("REVIEW_PROJECTS", *SETUP_INPUTS):
        env.pop(key, None)
    for key in list(env):
        if key.startswith("REVIEW_SECRET_"):
            del env[key]
    # The review child inherits the instance lock, so the lock is held until it exits even when the
    # poller is killed first. No replacement recovers this request while the review can still run.
    result = subprocess.run(argv, cwd=project.checkout, env=env, stdout=subprocess.PIPE,
                            pass_fds=(lock_fd,) if lock_fd is not None else ())
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return "failed"
    log.info("Review %s state=%s publication=%s provider=%s model_invocations=%s", address, report["state"],
             report.get("publication"), report.get("provider", "cursor"), report.get("model_invocations"))
    return report["state"] if report.get("publication") == "posted-or-already-present" else "failed"


def cycle(projects, state, jobs, since, runner=run_review):
    for project in projects.values():
        try:
            poll_project(project, state, jobs, since[project.key])
        except Exception:
            log.exception("Poll failed for %s; will retry next cycle", project.key)
    while job := jobs.next():
        key, number, comment = job
        status = "failed"
        if key in projects:
            try:
                status = runner(projects[key], number, state)
            except Exception:
                log.exception("Review failed for %s #%d comment %d", key, number, comment)
        jobs.finish(job, status)


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
    parser = argparse.ArgumentParser(description="Poll Assay for GitHub and GitLab /review comments")
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
        watch = ConfigurationWatch(state)
        try:
            fresh = not any((state / p).exists() for p in ("setup", "projects.json", "hooks.json"))
            first_install = (fresh and sys.stdin.isatty() and "REVIEW_PROJECTS" not in os.environ
                    and not any(k in os.environ for k in SETUP_INPUTS)
                    and not (Path.home() / ".config/assay/roster.env").exists())
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
            stack.enter_context(forge_credentials(projects, state))
        except (ValueError, KeyError, TypeError, EOFError, OSError, subprocess.TimeoutExpired) as err:
            parser.exit(2, f"Polling configuration: {err}\n")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        jobs = Jobs(state)
        since = {key: jobs.activated(key) for key in projects}
        log.info("Polling %s with %d seconds between cycles", ", ".join(projects), args.interval)
        if mine_seconds:
            threading.Thread(target=history.mine_periodically, args=(projects, state, mine_seconds),
                             name="qualgen-miner", daemon=True).start()
        while not watch.changed():
            if watch.enabled and any(p.managed and not p.checkout.is_dir() for p in projects.values()):
                watch.wait(2)
                continue
            cycle(projects, state, jobs, since)
            watch.wait(args.interval)


if __name__ == "__main__":
    main()
