"""Poll configured forges and process explicit review requests serially."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from .config import load_config
from .discovery import poll_project

log = logging.getLogger(__name__)


class Jobs:
    def __init__(self, state):
        self.path = state / "requests.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                project TEXT NOT NULL, number INTEGER NOT NULL, comment INTEGER NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(project, number, comment))""")
            db.execute("""CREATE TABLE IF NOT EXISTS activations (
                project TEXT PRIMARY KEY, since TEXT NOT NULL)""")

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
            db.execute("INSERT OR IGNORE INTO requests VALUES (?,?,?,?)",
                       (project, number, comment, status))

    def next(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute("""UPDATE requests SET status='started' WHERE rowid=(
                SELECT rowid FROM requests WHERE status='queued' ORDER BY rowid LIMIT 1)
                RETURNING project, number, comment""").fetchone()

    def finish(self, job, status):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE requests SET status=? WHERE project=? AND number=? AND comment=?",
                       (status, *job))


def run_review(project, number, state):
    address = f"{project.slug}#{number}"
    argv = [sys.executable, "-I", "-m", "cursor_pr.cli", "--repo", str(project.checkout),
            "--change", address, "--expected-change-url", project.change_url(number),
            "--post", "--cache-dir", str(state / "cache")]
    if os.getenv("REVIEW_MAX_INPUT_TOKENS"):
        argv.extend(["--max-input-tokens", os.environ["REVIEW_MAX_INPUT_TOKENS"]])
    if os.getenv("REVIEW_MODEL"):
        argv.extend(["--model", os.environ["REVIEW_MODEL"]])
    env = dict(os.environ)
    env.pop("REVIEW_PROJECTS", None)
    for key in list(env):
        if key.startswith("REVIEW_SECRET_"):
            del env[key]
    result = subprocess.run(argv, cwd=project.checkout, env=env, stdout=subprocess.PIPE)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return "failed"
    log.info("Review %s state=%s publication=%s cursor_invocations=%s", address, report["state"],
             report.get("publication"), report.get("cursor_invocations"))
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


def main():
    parser = argparse.ArgumentParser(description="Poll Assay for GitHub and GitLab /review comments")
    parser.add_argument("--state-dir", type=Path, default=os.environ.get(
        "REVIEW_STATE_DIR", str(Path.home() / ".local/state/cursor-review")))
    parser.add_argument("--interval", type=int, default=os.environ.get("REVIEW_POLL_SECONDS", "120"),
                        help="seconds to wait after each cycle (default 120)")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("poll interval must be positive")
    state = args.state_dir.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if int(os.getenv("REVIEW_MAX_INPUT_TOKENS", "6000")) < 1:
            raise ValueError("REVIEW_MAX_INPUT_TOKENS must be positive")
        projects = load_config(state)
    except (ValueError, KeyError, TypeError, EOFError, OSError) as err:
        parser.exit(2, f"Polling configuration: {err}\n")
    jobs = Jobs(state)
    since = {key: jobs.activated(key) for key in projects}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Polling %s with %d seconds between cycles", ", ".join(projects), args.interval)
    while True:
        cycle(projects, state, jobs, since)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
