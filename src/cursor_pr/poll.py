"""Poll configured forges and process explicit review requests serially."""
import argparse
import fcntl
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

from .config import DEFAULT_VISUAL_MAX_INPUT_TOKENS, load_config
from .credentials import SETUP_INPUTS, forge_credentials
from .discovery import COMMANDS, poll_project
from .manager import ConfigurationWatch
from .login import auth_mode, ensure_session, login_directory
from . import buildinfo, history, setup, providers

log = logging.getLogger(__name__)


class Jobs:
    def __init__(self, state):
        self.path = state / "requests.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                project TEXT NOT NULL, number INTEGER NOT NULL, comment INTEGER NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(project, number, comment))""")
            # Preserve the original queue shape so an older image can still use this volume.
            # Translate any queued visual requests from the initial visual-review schema.
            if "command" in {row[1] for row in db.execute("PRAGMA table_info(requests)")}:
                db.execute("""UPDATE requests SET status='visual-queued'
                              WHERE command='visual-review' AND status='queued'""")
                db.execute("ALTER TABLE requests DROP COLUMN command")
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

    def enqueue(self, project, number, comment, status="queued", command="review"):
        if status == "queued" and command == "visual-review":
            status = "visual-queued"  # An older poller claims only regular queued requests.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR IGNORE INTO requests VALUES (?,?,?,?)", (project, number, comment, status))

    def next(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute("""UPDATE requests SET status=
                    CASE status WHEN 'visual-queued' THEN 'visual-started' ELSE 'started' END
                WHERE rowid=(SELECT rowid FROM requests WHERE status IN ('queued', 'visual-queued')
                             ORDER BY rowid LIMIT 1)
                RETURNING project, number, comment,
                    CASE status WHEN 'visual-started' THEN 'visual-review' ELSE 'review' END""").fetchone()

    def finish(self, job, status):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE requests SET status=? WHERE project=? AND number=? AND comment=?",
                       (status, *job[:3]))


def run_review(project, number, state, command="review"):
    address = f"{project.slug}#{number}"
    argv = [sys.executable, "-I", "-m", "cursor_pr.cli", "--repo", str(project.checkout),
            "--change", address, "--expected-change-url", project.change_url(number),
            "--post", "--cache-dir", str(state / "cache")]
    if command not in COMMANDS.values():
        raise ValueError("Unknown review command")
    if command == "visual-review":
        argv.append("--visual")
    if history.interval():
        argv.extend(["--qualgen-store", str(history.store_for(state, project))])
    if project.scanner_ci:
        argv.extend(["--scanner-ci", json.dumps(project.scanner_ci)])
    if project.go_analysis:
        argv.extend(["--go-analysis-producer", project.go_analysis])
    # Each command has its own budget; the child resolves the saved one when the environment is silent.
    budget = "REVIEW_MAX_INPUT_TOKENS" if command == "review" else "REVIEW_VISUAL_MAX_INPUT_TOKENS"
    if os.getenv(budget):
        argv.extend(["--max-input-tokens", os.environ[budget]])
    if os.getenv("REVIEW_MODEL"):
        argv.extend(["--model", os.environ["REVIEW_MODEL"]])
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
    result = subprocess.run(argv, cwd=project.checkout, env=env, stdout=subprocess.PIPE)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return "failed"
    log.info("Review %s command=%s state=%s publication=%s provider=%s model_invocations=%s", address, command,
             report["state"], report.get("publication"), report.get("provider", "cursor"), report.get("model_invocations"))
    if report.get("publication") == "failed":
        log.warning("Review %s publication failed: %r", address,
                    report.get("publication_error", "reason not reported"))
    return report["state"] if report.get("publication") == "posted-or-already-present" else "failed"


def cycle(projects, state, jobs, since, runner=run_review):
    for project in projects.values():
        try:
            poll_project(project, jobs, since[project.key])
        except Exception:
            log.exception("Poll failed for %s; will retry next cycle", project.key)
    while job := jobs.next():
        key, number, comment, command = job
        status = "failed"
        if key in projects:
            try:
                status = runner(projects[key], number, state, command)
            except Exception:
                log.exception("Review failed for %s #%d comment %d", key, number, comment)
        jobs.finish(job, status)


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
    parser = argparse.ArgumentParser(description="Poll GitHub and GitLab for /review and /visual-review comments")
    parser.add_argument("--state-dir", type=Path, default=setup.state_dir())
    parser.add_argument("--interval", type=int,
                        help="seconds to wait after each cycle (default 120)")
    args = parser.parse_args()
    state = args.state_dir.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    watch = ConfigurationWatch(state)
    with ExitStack() as stack:
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
            setup.max_input_tokens(state, "REVIEW_VISUAL_MAX_INPUT_TOKENS", DEFAULT_VISUAL_MAX_INPUT_TOKENS)
            mine_seconds = history.interval()
            projects = load_config(state)
            stack.enter_context(providers.cursor_account())
            stack.enter_context(forge_credentials(projects))
        except (ValueError, KeyError, TypeError, EOFError, OSError, subprocess.TimeoutExpired) as err:
            parser.exit(2, f"Polling configuration: {err}\n")
        jobs = Jobs(state)
        since = {key: jobs.activated(key) for key in projects}
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
