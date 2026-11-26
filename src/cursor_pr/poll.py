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

from .config import load_config
from .credentials import SETUP_INPUTS, forge_credentials
from .discovery import poll_project
from .manager import ConfigurationWatch
from .login import auth_mode, ensure_session, login_directory
from . import buildinfo, history, setup, providers

log = logging.getLogger(__name__)


def head_record(state):
    # The head the running review resolved, from before its model call until its request ends. A
    # review child finding it repeats a request whose poller stopped, at that head or not at all.
    return state / "requests.head"


class Jobs:
    def __init__(self, state):
        self.path = state / "requests.sqlite3"
        self.state = state
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                project TEXT NOT NULL, number INTEGER NOT NULL, comment INTEGER NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(project, number, comment))""")
            if "attempts" not in {row[1] for row in db.execute("PRAGMA table_info(requests)")}:
                # Both in one transaction. A poller stopped in between would otherwise find the column
                # on its next start, skip the rest and repeat these reviews as though their heads could
                # be checked.
                db.execute("BEGIN")
                db.execute("ALTER TABLE requests ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
                # No earlier poller recorded the head a running review had resolved, so repeating it
                # could review a head nobody asked for. It is given up on instead.
                for key, number, comment in db.execute(
                        "SELECT project, number, comment FROM requests WHERE status='started'").fetchall():
                    log.warning("Review of %s #%d comment %d was interrupted by a restart; marked failed, "
                                "the head it started at was not recorded, a new /review retries it",
                                key, number, comment)
                db.execute("UPDATE requests SET status='failed' WHERE status='started'")
            db.execute("""CREATE TABLE IF NOT EXISTS activations (
                project TEXT PRIMARY KEY, since TEXT NOT NULL)""")
            # Only the process holding the instance lock opens the queue, so a row still started
            # was left by a review that died with the previous process. Run it once more; a
            # request that keeps killing the poller must not loop.
            for key, number, comment, attempts in db.execute(
                    "SELECT project, number, comment, attempts FROM requests WHERE status='started'").fetchall():
                status = "queued" if attempts < 2 else "failed"
                db.execute("UPDATE requests SET status=? WHERE project=? AND number=? AND comment=?",
                           (status, key, number, comment))
                log.warning("Review of %s #%d comment %d was interrupted by a restart; %s", key, number, comment,
                            "queued again" if status == "queued" else "marked failed, a new /review retries it")

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
            job = db.execute("""UPDATE requests SET status='started', attempts=attempts+1 WHERE rowid=(
                SELECT rowid FROM requests WHERE status='queued' ORDER BY rowid LIMIT 1)
                RETURNING project, number, comment, attempts""").fetchone()
        if job and job[3] == 1:
            # A first start: a head left by an earlier request is not this one's.
            head_record(self.state).unlink(missing_ok=True)
        return job[:3] if job else None

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
    record = head_record(state)
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
    try:
        # The head of a start a restart interrupted, which this one must stay at.
        argv.extend(["--expected-head", record.read_text().strip()])
    except FileNotFoundError:
        argv.extend(["--record-head", str(record)])
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
    result = subprocess.run(argv, cwd=project.checkout, env=env, stdout=subprocess.PIPE)
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
    fd = os.open(state / ".poller.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
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
