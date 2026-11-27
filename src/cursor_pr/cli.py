import argparse
from collections import defaultdict
import fcntl
from contextlib import ExitStack, closing, contextmanager
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

from unidiff.errors import UnidiffParseError
import yaml

from . import drift, history, providers, scanners
from .scanners import fence
from .login import auth_mode, login_directory
from .setup import locked, apply_saved_setup, load_provider_key, max_input_tokens, state_dir
from .config import DEFAULT_MAX_INPUT_TOKENS, load_config, project_from
from .credentials import forge_credentials
from .artifacts import forge_go_report
from .checks import go_report, scan_files
from .sync import scan_checkout
from .engine import CursorFailure, pack, render_sections, review, tokens
from .source import ReviewError, ci_refs, forge_change, local_change, post_comment, valid_sha


LOCKS = {"package-lock.json", "pnpm-lock.yaml", "go.sum", "npm-shrinkwrap.json"}
MAX_DEGRADED_ROWS = 50


def rank(path):
    """Order in which whole files leave an oversized prompt: the least reviewable first."""
    parts = path.lower().split("/")
    name = parts[-1]
    if name in LOCKS or name.endswith((".lock", ".min.js", ".min.css")):
        return 0
    if (set(parts[:-1]) & {"test", "tests", "__tests__", "testdata", "spec"} or name.startswith("test_")
            or re.search(r"(_test|\.test|\.spec)\.[a-z0-9]+$", name)):
        return 1
    if "docs" in parts[:-1] or name.endswith((".md", ".rst", ".txt", ".adoc")):
        return 2
    return 3


def fit(files, checks, limit):
    """Pack the prompt, leaving out whole files until it fits the input budget.

    Returns the prompt parts, the estimate of what will be sent, the estimate of the full
    prompt, and the paths left out. Nothing is left out when even one file cannot fit.
    """
    rendered = render_sections(files)
    prompt, anchors, unseen, has_text = full_pack = pack(files, checks, rendered=rendered)
    estimated = full = tokens(prompt) if has_text else 0
    if full <= limit:
        return full_pack, full, full, []
    # Measure each file with its scanner evidence before choosing what to leave out.
    # A file that cannot fit alone leaves first, so a smaller file can still be reviewed.
    findings_by_path = defaultdict(list)
    for check in checks:
        for finding in check["findings"]:
            findings_by_path[finding["path"]].append(finding)
    overhead = tokens(pack([], [])[0])
    sizes = {}
    for file in files:
        if rendered[file.path][0] is not None:
            file_checks = [{"findings": findings_by_path.get(file.path, ())}]
            standalone, _, _, _ = pack([file], file_checks, rendered=rendered)
            sizes[file.path] = tokens(standalone) - overhead
    queue = sorted(sizes, key=lambda path: (overhead + sizes[path] <= limit, rank(path), -sizes[path], path))
    dropped = []
    while estimated > limit and queue:
        # Per-file sizes are approximate, so the last file only leaves after a real recount.
        dropped.append(queue.pop(0))
        freed = sizes[dropped[-1]]
        while len(queue) > 1 and freed < estimated - limit:
            dropped.append(queue.pop(0))
            freed += sizes[dropped[-1]]
        prompt, anchors, unseen, has_text = pack(files, checks, frozenset(dropped), rendered=rendered)
        estimated = tokens(prompt) if has_text else 0
    if not has_text or estimated > limit:
        return full_pack, full, full, []
    return (prompt, anchors, unseen, has_text), estimated, full, sorted(dropped)


def publish(address, head, report, *, project=None, state=None):
    _, number = address.rsplit("#", 1)
    label = providers.LABELS[report.get("provider", "cursor")]
    body = [f"{label} review of {head}", report["state"]]
    degraded = report.get("degraded") or []
    if degraded:
        body.append(f"Degraded review: the full prompt is an estimated {report['full_input_tokens']} tokens, "
                    f"over the limit of {report['max_input_tokens']}. {label} did not review these files:")
        body.extend(f"- Not reviewed (token limit): {fence(path)}" for path in degraded[:MAX_DEGRADED_ROWS])
        if len(degraded) > MAX_DEGRADED_ROWS:
            body.append(f"- Not reviewed (token limit): {len(degraded) - MAX_DEGRADED_ROWS} more files")
    # Paths come from the PR and titles/messages from the model: quote them as code
    # so the reviewer identity never posts a Markdown link or @mention for an attacker.
    for issue in report["findings"]:
        body.append(f"- {fence(issue['path'])}:{issue['line']} — {fence(issue['title'])}. {fence(issue['message'])}")
    if report["state"] == "over-budget":
        body.append(f"Skipped {label}. Estimated prompt {report['estimated_input_tokens']} tokens "
                    f"exceeds the limit of {report['max_input_tokens']}.")
    elif report["state"] == "model-unavailable":
        body.append(f"{label} could not complete the review.")
    elif not report["findings"]:
        body.append("No model findings in the reviewed scope.")
    for check in report["checks"]:
        if check["state"] != "complete":
            body.append(f"- {check['tool']}: could not check {fence(check.get('path') or check.get('reason', ''))}")
        for item in check["findings"]:
            detail = " ".join(part for part in (fence(item["rule"]), fence(item.get("message", ""))) if part)
            body.append(f"- {fence(item['path'])}:{item['line']} — {detail}")
    if report.get("static_checks"):
        body.extend(["", *scanners.summary(report["static_checks"]), ""])
    if "qualgen" in report:
        body.extend(["", *history.summary(report["qualgen"]), ""])
    if "drift" in report:
        body.extend(["", *drift.summary(report["drift"]), ""])
    for path in report["omitted"]:
        body.append(f"- Outside review coverage: {fence(path)}")
    body.extend(["", "Review tooling built on [PR-Agent](https://github.com/the-pr-agent/pr-agent)."])
    # A finding may quote a multiline source fragment, including forge quick actions.
    text = "\n".join(" ".join(line.splitlines()) for line in body) + "\n"
    post_comment(project, int(number), head, text, state if state is not None else state_dir())


class Stopped(SystemExit):
    pass


# Set once the supervisor has asked this review to stop.
stopping = False


def stopped(number, frame):
    # The supervisor asks before it ends the review. Leaving like any exit removes the temporary
    # files and ends the command being waited on. The row is not settled and stays started.
    global stopping
    if stopping:
        return  # Asked again while already leaving. Raising a second time would cut the cleanup short.
    stopping = True
    raise Stopped("cursor-review: stopped by its supervisor")


def request_row(args):
    project, number, comment = args.request
    return project, int(number), int(comment)


def open_queue(args):
    # The poller's queue as it is. A wrong path is an error, not a new empty database.
    return closing(sqlite3.connect(args.queue.resolve().as_uri() + "?mode=rw", uri=True))


def claim_head(args, head):
    # The row of the request under review takes the head before any other work. A repeat of the
    # request finds the head of its interrupted start there and is refused at another. Read and
    # written in one transaction on the row the poller reads, so the two cannot disagree.
    try:
        with open_queue(args) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT head, prepared FROM requests WHERE project=? AND number=? AND comment=?
                AND status='started'""", request_row(args)).fetchone()
            if row is None:
                raise ReviewError("The queue has no running request for this review")
            if row[0] and row[0] != head:
                raise ReviewError(f"Change is at {head}, not at {row[0]}, the head of its interrupted start")
            db.execute("UPDATE requests SET head=? WHERE project=? AND number=? AND comment=?",
                       (head, *request_row(args)))
            return row[1]
    except sqlite3.Error as err:
        raise ReviewError(f"Review queue: {err}") from err


def prepare_publication(args, report, code):
    # A restart can retry the same publication without repeating a model call or changing its text.
    try:
        with open_queue(args) as db, db:
            db.execute("""UPDATE requests SET prepared=? WHERE project=? AND number=? AND comment=?
                AND status='started' AND head=? AND prepared IS NULL""",
                       (json.dumps([report, code]), *request_row(args), report["head"]))
    except sqlite3.Error as err:
        raise ReviewError(f"Review queue: {err}") from err


def settle(args, status):
    # How the review ended, on the request's row and before the lock is released, so that a poller
    # that has stopped reading finds it there. The first one stands: a report that only failed to
    # print did not fail. Never raises, because it also runs while a failure is being reported.
    if not getattr(args, "own_row", False):
        return  # Not shown to be this review's row. The failure being reported is what stands.
    try:
        with open_queue(args) as db, db:
            db.execute("""UPDATE requests SET status=?, prepared=NULL WHERE project=? AND number=? AND comment=?
                AND status='started'""", (status, *request_row(args)))
    except (sqlite3.Error, ValueError, OSError) as err:
        print(f"cursor-review: could not record how the review ended: {err}", file=sys.stderr)


def review_outcome(report):
    # What the poller keeps for a finished review: its state once its result is on the change.
    return report["state"] if report.get("publication") == "posted-or-already-present" else "failed"


def parser():
    p = argparse.ArgumentParser(description="Run one bounded code review and emit a JSON report.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--change", help="Configured GitHub PR or GitLab MR, e.g. 'org/repo#42'")
    mode.add_argument("--base", help="Local base revision to compare against")
    mode.add_argument("--ci", action="store_true", help="Use provider CI event SHAs in the checkout")
    p.add_argument("--expected-change-url", help="Require this exact open forge change before reviewing")
    p.add_argument("--queue", type=Path, help="The poller's request queue, with --request")
    p.add_argument("--request", nargs=3, metavar=("PROJECT", "NUMBER", "COMMENT"),
                   help="The queued request under review: its row takes the head and how the review ended")
    p.add_argument("--head", help="Local head revision (default HEAD with --base)")
    p.add_argument("--repo", type=Path, default=Path.cwd(), help="Checkout for local Git and scanners")
    p.add_argument("--path", action="append", default=[], help="Limit review to an exact changed path; repeatable")
    evidence = p.add_mutually_exclusive_group()
    evidence.add_argument("--go-analysis-producer", help="Trusted CI producer for --expected-change-url")
    evidence.add_argument("--go-report", type=Path, help="Assay go-analysis/v1 JSON for this head")
    p.add_argument("--scanner-ci", type=json.loads, help="Per-tool trusted CI report settings for a forge change")
    p.add_argument("--qualgen-store", type=Path, help="Read the poller’s mined Qualgen history for this project")
    p.add_argument("--model", help="Explicit provider model (otherwise the CLI default)")
    p.add_argument("--max-input-tokens", type=int,
                   help=f"Preflight estimate limit (default {DEFAULT_MAX_INPUT_TOKENS}, or the saved REVIEW_MAX_INPUT_TOKENS setting)")
    p.add_argument("--estimate", action="store_true", help="Prepare the inputs and report cost estimate without calling the model")
    p.add_argument("--cache-dir", type=Path, default=Path(os.getenv("XDG_CACHE_HOME", Path.home()/".cache"))/"cursor-pr-review")
    p.add_argument("--post", action="store_true", help="Publish a head-bound summary with --change")
    return p


def execute(args):
    if args.max_input_tokens < 1 or (args.head and not args.base) or (args.post and (not args.change or args.estimate)):
        raise ReviewError("Invalid budget or incompatible --head/--post options")
    if bool(args.queue) != bool(args.request) or (args.request and not args.change):
        raise ReviewError("--queue and --request go together and require --change")
    root = args.repo.resolve()
    if args.expected_change_url and not args.change:
        raise ReviewError("--expected-change-url requires --change")
    if args.go_analysis_producer and not args.expected_change_url:
        raise ReviewError("--go-analysis-producer requires --expected-change-url")
    if args.change:
        change = forge_change(args.change, args.expected_change_url, project=getattr(args, "forge_project", None))
    else:
        base, head = ci_refs() if args.ci else (args.base, args.head or "HEAD")
        change = local_change(root, base, head)
        drift_base = change.base
        github_pr = os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("GITHUB_EVENT_NAME") == "pull_request"
        if args.ci and not github_pr and os.getenv("CI_PIPELINE_SOURCE") == "merge_request_event":
            target = os.getenv("CI_MERGE_REQUEST_TARGET_BRANCH_SHA")
            drift_base = target if valid_sha(target) else None

    if args.request:
        if not getattr(args, "own_row", False):
            raise ReviewError("The request's row was not checked against the change")
        prepared = claim_head(args, change.head)
        if prepared is not None:
            report, code = json.loads(prepared)
            return report, code

    paths = {f.path for f in change.files}
    selected = set(args.path) if args.path else paths
    if not selected <= paths:
        raise ReviewError("--path must name a file changed in this review")
    files = sorted((f for f in change.files if f.path in selected), key=lambda f: f.path)
    project = getattr(args, "managed_project", None)
    wanted = scanners.inputs(files) if args.change else set()
    static = scanners.ci_results(args.expected_change_url, change.head, root, wanted, args.scanner_ci or {})
    missing = sorted(wanted - static.keys())
    with ExitStack() as stack:
        checkout = root
        # A managed checkout holds only the default branch: fetch the head for scans and base drift.
        if project:
            number = args.change.rsplit("#", 1)[1]
            if args.expected_change_url != project.change_url(number):
                raise ReviewError("Managed checkout and reviewed project differ")
            checkout = stack.enter_context(scan_checkout(root, project, number, change.head))
        checks = scan_files(checkout, change.head, files)
        deadline = time.monotonic() + 180
        for tool, path in missing:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                static[tool, path] = scanners.local(checkout, change.head, tool, path, state_dir() / "osv-db",
                                                    timeout=min(90, remaining))
            else:
                static[tool, path] = {"tool": tool, "path": path, "source": "local", "state": "could-not-check",
                                      "findings": [], "reason": "static check time budget exhausted"}
        if args.change:
            drifted = drift.forge(root, checkout, change, bool(project), files)
        else:
            drifted = drift.check(root, change.head, drift_base, files)
    go = None
    if args.go_report:
        go = go_report(args.go_report, change.head)
    elif args.go_analysis_producer:
        go = forge_go_report(change.identity, args.go_analysis_producer, change.head, root)
    if go is not None:
        go["findings"] = [row for row in go["findings"] if row["path"] in selected]
        checks.append(go)

    (prompt, anchors, unseen, has_text), estimated, full, degraded = fit(files, checks, args.max_input_tokens)
    omitted = sorted(set(unseen) | (paths - selected))
    backend = providers.provider()
    report = {"provider": backend, "source": change.identity, "head": change.head, "model": args.model or f"{backend}-default",
              "estimated_input_tokens": estimated, "max_input_tokens": args.max_input_tokens,
              "cursor_invocations": 0, "cache_hit": False, "omitted": omitted, "checks": checks,
              "findings": [], "cursor_usage": None, "full_input_tokens": full, "degraded": degraded,
              "drift": drifted}
    if static:
        report["static_checks"] = [static[key] for key in sorted(static)]
    if backend == "cursor" and os.getenv("REVIEW_CURSOR_TEAM_ID"):
        report["cursor_team_id"] = os.environ["REVIEW_CURSOR_TEAM_ID"]
    if args.qualgen_store:
        report["qualgen"] = history.screen(root, args.qualgen_store, selected, change.head)
    if estimated > args.max_input_tokens:
        return dict(report, state="over-budget"), 2
    if args.estimate:
        return dict(report, state="estimate"), 2 if degraded else 0
    if has_text:
        try:
            answer, cached = review(prompt, args.model, anchors, args.cache_dir,
                                    change.identity)
        except CursorFailure as err:
            return dict(report, state="model-unavailable", error=str(err),
                        cursor_invocations=err.invocations, cursor_usage=err.usage), 2
        except (ReviewError, OSError, ValueError, KeyError, TypeError, AttributeError,
                subprocess.TimeoutExpired, yaml.YAMLError) as err:
            # Preserve deterministic diagnostics even if the model failed.
            return dict(report, state="model-unavailable", error=str(err) if isinstance(err, ReviewError)
                        else type(err).__name__, cursor_invocations=None), 2
        report.update(answer, cache_hit=cached, cursor_invocations=int(not cached))
        if cached:
            report["cursor_usage"] = None
    incomplete = bool(omitted or any(c["state"] != "complete" for c in [*checks, *static.values()]))
    if degraded:
        report["state"] = "degraded"
    else:
        report["state"] = "partial" if incomplete else ("reviewed" if has_text else "empty")
    return report, 2 if incomplete or degraded else 0


def run_request(args):
    report, code = execute(args)
    if args.request and args.post:
        prepare_publication(args, report, code)
    backend = report["provider"]
    if backend != "cursor":
        report["model_invocations"] = report["cursor_invocations"]
        report["model_usage"] = report["cursor_usage"]
        report.update(cursor_invocations=0, cursor_usage=None)
    else:
        report.update(model_invocations=report["cursor_invocations"], model_usage=report["cursor_usage"])
    if args.post:
        try:
            publish(args.change, report["head"], report, project=getattr(args, "forge_project", None),
                    state=getattr(args, "forge_state", None))
        except (ReviewError, OSError, ValueError, subprocess.TimeoutExpired) as err:
            return dict(report, publication="failed", publication_error=str(err) or type(err).__name__), 2
        report["publication"] = "posted-or-already-present"
    return report, code


def main():
    options = parser()
    args = options.parse_args()
    # Refused here, before this process may settle a row: a mistaken call must not end a request
    # that another review is running.
    if bool(args.queue) != bool(args.request) or (args.request and not args.change):
        options.error("--queue and --request go together and require --change")
    if args.request:
        try:
            request_row(args)
        except ValueError:
            options.error("--request takes a project, a change number and a comment number")
    # Load PR-Agent settings only from an empty directory, independent of the target repo.
    args.repo = args.repo.resolve()
    args.cache_dir = args.cache_dir.resolve()
    if args.queue:
        args.queue = args.queue.resolve()  # Before the working directory changes.
    if args.go_report:
        args.go_report = args.go_report.resolve()
    if args.qualgen_store:
        args.qualgen_store = args.qualgen_store.resolve()
    old = Path.cwd()
    global stopping
    stopping = False
    asked = signal.signal(signal.SIGTERM, stopped) if args.request else None
    try:
        with ExitStack() as stack:
            state = state_dir().resolve()
            if args.change and os.getenv("CURSOR_REVIEW_POLLER_CHILD") != "1":
                apply_saved_setup(state)
            elif not args.change and auth_mode(providers.provider()) == "api-key":
                load_provider_key(providers.provider())
            if os.getenv("REVIEW_LOGIN_PROFILE") and auth_mode(providers.provider()) == "login":
                stack.enter_context(locked(login_directory(state), fcntl.LOCK_SH))
            if not args.estimate and os.getenv("CURSOR_REVIEW_POLLER_CHILD") != "1":
                stack.enter_context(providers.cursor_account())
            if args.max_input_tokens is None:
                args.max_input_tokens = max_input_tokens(state)
            if args.model is None:
                args.model = os.getenv("REVIEW_MODEL")
            if args.change:
                if os.getenv("CURSOR_REVIEW_POLLER_CHILD") == "1":
                    project = project_from(json.loads(os.environ["REVIEW_FORGE_PROJECT"]))
                else:
                    state.mkdir(mode=0o700, parents=True, exist_ok=True)
                    projects = load_config(state)
                    slug = args.change.rpartition("#")[0]
                    project = next((p for p in projects.values() if p.slug == slug), None)
                    if project is None:
                        raise ReviewError("Use a --change address from the configured review projects")
                    stack.enter_context(forge_credentials(projects))
                slug, _, number = args.change.rpartition("#")
                if project.slug != slug or not number.isascii() or not number.isdecimal():
                    raise ReviewError("Use a --change address from the configured review projects")
                if args.expected_change_url and args.expected_change_url != project.change_url(number):
                    raise ReviewError("Configured project and expected change URL differ")
                args.expected_change_url = project.change_url(number)
                if args.scanner_ci is None:
                    args.scanner_ci = project.scanner_ci
                if args.request:
                    # The row is this review's own only if it is for this change. Until that is
                    # known, nothing here settles it: it may be the row of another, running review.
                    if request_row(args)[:2] != (project.key, int(number)):
                        raise ReviewError("--request names another change than --change")
                    args.own_row = True
                args.forge_project = project
                args.forge_state = state
                args.managed_project = project if project.managed else None
            clean = stack.enter_context(tempfile.TemporaryDirectory(prefix="pr-agent-settings-"))
            os.chdir(clean)
            from pr_agent.algo.git_patch_processing import to_hunk_only_patch  # noqa: F401
            from pr_agent.algo.token_handler import TokenEncoder  # noqa: F401
            os.chdir(args.repo)
            report, status = run_request(args)
            if args.request:
                # At once, before the cleanup or the report can fail.
                settle(args, review_outcome(report))
        print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
        return status
    except (ReviewError, OSError, ValueError, TypeError, KeyError, AttributeError,
            UnidiffParseError, yaml.YAMLError, subprocess.TimeoutExpired) as err:
        if stopping:
            # Something failed while leaving because the supervisor asked. That is still an interrupted
            # review, whose row stays started, not one that ran and failed.
            raise Stopped(f"cursor-review: stopped by its supervisor ({err})") from err
        if args.request:
            # Before the diagnostic, which fails when nobody reads it any more.
            settle(args, "failed")
        print(f"cursor-review: {err}", file=sys.stderr)
        return 2
    finally:
        if args.request:
            signal.signal(signal.SIGTERM, asked)
        os.chdir(old)


if __name__ == "__main__":
    sys.exit(main())
