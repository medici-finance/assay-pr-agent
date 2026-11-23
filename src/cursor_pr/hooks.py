"""Receive forge comments and run one requested review at a time."""
import argparse
from contextlib import asynccontextmanager, closing
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
import uvicorn

from .hook_config import load_config
from .hook_events import authenticated, command

log = logging.getLogger("uvicorn.error")


class Jobs:
    def __init__(self, state):
        self.path = state / "requests.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                project TEXT NOT NULL, number INTEGER NOT NULL, comment INTEGER NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(project, number, comment))""")
        self.wake = threading.Event()

    def enqueue(self, project, number, comment):
        with closing(sqlite3.connect(self.path)) as db, db:
            inserted = db.execute("INSERT OR IGNORE INTO requests VALUES (?,?,?,'queued')",
                                  (project, number, comment)).rowcount
        if inserted:
            self.wake.set()
        return bool(inserted)

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


def make_app(projects, state, runner=run_review):
    jobs = Jobs(state)
    stop = threading.Event()

    def work():
        while not stop.is_set():
            jobs.wake.clear()
            job = jobs.next()
            if not job:
                jobs.wake.wait()
                continue
            key, number, comment = job
            try:
                status = runner(projects[key], number, state)
            except Exception:
                log.exception("Review failed for %s #%d comment %d", key, number, comment)
                status = "failed"
            jobs.finish(job, status)

    @asynccontextmanager
    async def lifespan(app):
        thread = threading.Thread(target=work, name="requested-reviews", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            jobs.wake.set()
            thread.join(timeout=1)

    async def receive(request):
        project = projects.get(request.path_params["key"])
        if project is None:
            return Response(status_code=404)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 2_000_000:
                return Response(status_code=413)
            body.extend(chunk)
        if not authenticated(project, request.headers, body):
            return Response(status_code=403)
        header = "x-github-event" if project.provider == "github" else "x-gitlab-event"
        try:
            found = command(project, request.headers.get(header), json.loads(body))
        except (ValueError, TypeError, AttributeError, KeyError):
            return Response(status_code=400)
        if found and jobs.enqueue(project.key, *found):
            return Response(status_code=202)
        return Response(status_code=204)

    return Starlette(routes=[Route("/hooks/{key:path}", receive, methods=["POST"])], lifespan=lifespan)


def main():
    parser = argparse.ArgumentParser(description="Serve on-demand GitHub and GitLab /review hooks")
    parser.add_argument("--state-dir", type=Path, default=os.environ.get(
        "REVIEW_STATE_DIR", str(Path.home() / ".local/state/cursor-review")))
    parser.add_argument("--bind", default=os.environ.get("REVIEW_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.environ.get("REVIEW_PORT", "8787"))
    args = parser.parse_args()
    state = args.state_dir.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if int(os.getenv("REVIEW_MAX_INPUT_TOKENS", "6000")) < 1:
            raise ValueError("REVIEW_MAX_INPUT_TOKENS must be positive")
        public, projects = load_config(state)
    except (ValueError, KeyError, TypeError, EOFError, OSError) as err:
        parser.exit(2, f"Webhook configuration: {err}\n")
    for key in projects:
        print(f"{projects[key].provider} comment webhook: {public}/hooks/{key}", flush=True)
    uvicorn.run(make_app(projects, state), host=args.bind, port=args.port)


if __name__ == "__main__":
    main()
