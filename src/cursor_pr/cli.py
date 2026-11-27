import argparse
import fcntl
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

from unidiff.errors import UnidiffParseError
import yaml

from . import drift, history, outcomes, providers, receipt, scanners
from .scanners import fence
from .login import auth_mode, login_directory
from .setup import locked, apply_saved_setup, load_provider_key, max_input_tokens, state_dir
from .config import DEFAULT_MAX_INPUT_TOKENS, load_config, project_from
from .credentials import forge_credentials
from .artifacts import forge_go_report
from .checks import go_report, scan_files
from .sync import scan_checkout
from .engine import ModelFailure, encodings, pack, review, section, tokens
from .source import ReviewError, ci_refs, forge_change, local_change, post_comment


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


def fit(files, checks, limit, *, title="", description="", advisories=(), scoped=False, model=None):
    """Pack the prompt, leaving out whole files until it fits the input budget.

    Returns the prompt parts, the estimate of what will be sent, the estimate of the full
    prompt, and the paths left out. Nothing is left out when even one file cannot fit.
    """
    prompt, anchors, no_text, has_text = full_pack = pack(files, checks, title=title, description=description,
                                                         advisories=advisories, scoped=scoped)
    estimated = full = tokens(prompt, model) if has_text else 0
    if full <= limit:
        return full_pack, full, full, []
    model_encodings = encodings(model)
    counters = (model,) if len(model_encodings) == 1 else model_encodings

    def counts(text):
        return tuple(tokens(text, counter) for counter in counters)

    file_counts = {f.path: counts(text) for f in files if (text := section(f)[0])}
    sizes = {path: max(counts) for path, counts in file_counts.items()}
    # A file that cannot fit even alone leaves first, so it never takes a file that fits with it.
    overhead = counts(pack(files, checks, frozenset(sizes), title, description, advisories, scoped)[0])
    fits_alone = {path for path, counts in file_counts.items()
                  if all(base + extra <= limit for base, extra in zip(overhead, counts, strict=True))}
    queue = sorted(sizes, key=lambda path: (path in fits_alone, rank(path), -sizes[path], path))
    dropped = []
    while estimated > limit and queue:
        # Per-file sizes are approximate, so the last file only leaves after a real recount.
        dropped.append(queue.pop(0))
        freed = sizes[dropped[-1]]
        while len(queue) > 1 and freed < estimated - limit:
            dropped.append(queue.pop(0))
            freed += sizes[dropped[-1]]
        prompt, anchors, no_text, has_text = pack(files, checks, frozenset(dropped), title, description, advisories, scoped)
        estimated = tokens(prompt, model) if has_text else 0
    if not has_text or estimated > limit:
        return full_pack, full, full, []
    return (prompt, anchors, no_text, has_text), estimated, full, sorted(dropped)


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
    # A dropped finding is disclosed by count and reason only: its text was not evidence-bound.
    for reason in sorted({item["reason"] for item in report.get("dropped", [])}):
        count = sum(item["reason"] == reason for item in report["dropped"])
        body.append(f"Dropped {count} model finding{'s' if count > 1 else ''}: {reason}.")
    unchecked = sum(entry.get("evidence") == "unverified" for entry in report["findings"])
    if unchecked:
        body.append(f"{unchecked} model finding{'s' if unchecked > 1 else ''} quoted no code to check against the patch.")
    if report["state"] == "over-budget":
        body.append(f"Skipped {label}. Estimated prompt {report['estimated_input_tokens']} tokens "
                    f"exceeds the limit of {report['max_input_tokens']}.")
    elif report["state"] == "model-unavailable":
        body.append(f"{label} could not complete the review.")
    elif not report["findings"]:
        # With every model finding dropped, saying there were none would read as a clean review.
        body.append("No model finding passed the checks." if report.get("dropped")
                    else "No model findings in the reviewed scope.")
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
    if report.get("not_reviewable"):
        # Paths come from the PR: quote them as code.
        more = f" and {len(report['not_reviewable']) - 20} more" if len(report["not_reviewable"]) > 20 else ""
        body.append("- Not reviewed, no text to review: "
                    + ", ".join(fence(path) for path in report["not_reviewable"][:20]) + more + ".")
    for path in report["omitted"]:
        body.append(f"- Outside review coverage: {fence(path)}")
    tracked = report.get("outcomes")
    if tracked and tracked["fixed"] + tracked["unaddressed"] + tracked["repeated"]:
        # An accept rate needs a finding whose code the author either changed or left alone.
        rate = f"{tracked['accept_rate']}%" if tracked["accept_rate"] is not None else "not available"
        body.append(f"Tracked findings for this change: {tracked['fixed']} addressed and {tracked['unaddressed']} left unchanged "
                    f"at a later review (accept rate {rate}); {tracked['repeated']} raised again; "
                    f"{tracked['open']} awaiting a later review.")
    body.extend(["", "Review tooling built on [PR-Agent](https://github.com/the-pr-agent/pr-agent)."])
    # A finding may quote a multiline source fragment, including forge quick actions.
    lines = [" ".join(line.splitlines()) for line in body]
    destination = state if state is not None else state_dir()
    dedupe_body = "\n".join(lines) + "\n"
    if "receipt" in report:
        # Retrying a post may hit the model cache and change the receipt, but the review is the same.
        lines[-2:-2] = ["", *receipt.render(report["receipt"])]
        post_comment(project, int(number), head, "\n".join(lines) + "\n", destination, dedupe_body=dedupe_body)
    else:
        post_comment(project, int(number), head, dedupe_body, destination)


def has_reviewable_text(file):
    """An unavailable or malformed patch cannot establish that the file has no text."""
    if file.patch_unavailable:
        return True
    try:
        return bool(section(file)[1])
    except (ReviewError, UnidiffParseError):
        return True


def parser():
    p = argparse.ArgumentParser(description="Run one bounded code review and emit a JSON report.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--change", help="Configured GitHub PR or GitLab MR, e.g. 'org/repo#42'")
    mode.add_argument("--base", help="Local base revision to compare against")
    mode.add_argument("--ci", action="store_true", help="Use provider CI event SHAs in the checkout")
    p.add_argument("--expected-change-url", help="Require this exact open forge change before reviewing")
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
            drifted = drift.check(root, change.head, change.base, files)
    go = None
    if args.go_report:
        go = go_report(args.go_report, change.head)
    elif args.go_analysis_producer:
        go = forge_go_report(change.identity, args.go_analysis_producer, change.head, root)
    if go is not None:
        go["findings"] = [row for row in go["findings"] if row["path"] in selected]
        checks.append(go)

    # Screened before the prompt is built: the advisories are part of what the model is shown.
    qualgen = history.screen(root, args.qualgen_store, selected, change.head) if args.qualgen_store else None
    (prompt, anchors, no_text, has_text), estimated, full, degraded = fit(
        files, checks, args.max_input_tokens, title=change.title, description=change.description,
        advisories=qualgen["advisories"] if qualgen else (), scoped=selected != paths, model=args.model)
    # An absent forge patch does not establish that the file has no text.
    unavailable = {f.path for f in files if f.patch_unavailable}
    omitted = sorted((paths - selected) | unavailable)
    backend = providers.provider()
    report = {"provider": backend, "source": change.identity, "head": change.head, "model": args.model or f"{backend}-default",
              "estimated_input_tokens": estimated, "max_input_tokens": args.max_input_tokens,
              "model_invocations": 0, "cache_hit": False, "omitted": omitted,
              "not_reviewable": [path for path in no_text if path not in unavailable],
              "checks": checks,
              "findings": [], "model_usage": None, "full_input_tokens": full, "degraded": degraded,
              "drift": drifted}
    deselected = [f for f in change.files if f.path not in selected]
    known_no_text = len(report["not_reviewable"]) + sum(not has_reviewable_text(f) for f in deselected)
    prompt_files = len(files) - len(no_text)
    report["coverage"] = {"changed": len(paths), "text": len(paths) - known_no_text, "reviewed": 0,
                          "skipped": prompt_files if estimated > args.max_input_tokens else len(degraded),
                          "excluded": len(deselected), "no_text": known_no_text,
                          "unavailable": len(unavailable) + sum(f.patch_unavailable for f in deselected)}
    if static:
        report["static_checks"] = [static[key] for key in sorted(static)]
    if backend == "cursor" and os.getenv("REVIEW_CURSOR_TEAM_ID"):
        report["cursor_team_id"] = os.environ["REVIEW_CURSOR_TEAM_ID"]
    if qualgen:
        report["qualgen"] = qualgen
    if estimated > args.max_input_tokens:
        return dict(report, state="over-budget"), 2, None
    if args.estimate:
        return dict(report, state="estimate"), 2 if degraded or unavailable else 0, None
    if has_text:
        try:
            answer, cached = review(prompt, args.model, anchors, args.cache_dir,
                                    change.identity)
        except ModelFailure as err:
            return dict(report, state="model-unavailable", error=str(err),
                        model_invocations=err.invocations, model_usage=err.usage), 2, None
        except (ReviewError, OSError, ValueError, KeyError, TypeError, AttributeError,
                subprocess.TimeoutExpired, yaml.YAMLError) as err:
            # Preserve deterministic diagnostics even if the model failed.
            return dict(report, state="model-unavailable", error=str(err) if isinstance(err, ReviewError)
                        else type(err).__name__, model_invocations=None), 2, None
        # A result cached before the keys were renamed still carries the old one.
        report.update(answer, cache_hit=cached, model_invocations=int(not cached))
        report["coverage"]["reviewed"] = prompt_files - len(degraded)
        report.pop("cursor_usage", None)
        if cached:
            report["model_usage"] = None
    incomplete = bool(omitted or any(c["state"] != "complete" for c in [*checks, *static.values()]))
    if degraded:
        report["state"] = "degraded"
    else:
        report["state"] = "partial" if incomplete else ("reviewed" if has_text else "empty")
    return report, 2 if incomplete or degraded else 0, (anchors, paths)


def track(report, tracking, *, persist):
    # Tracking is best effort. A failure must never prevent a review from being posted.
    try:
        report["outcomes"] = outcomes.record(*tracking, persist=persist)
    except (OSError, ValueError, KeyError, sqlite3.Error):
        report.pop("outcomes", None)


def run_request(args):
    report, code, scope = execute(args)
    # Preserve the legacy JSON fields for consumers that have not migrated to model_*.
    cursor = report["provider"] == "cursor"
    report["cursor_invocations"] = report["model_invocations"] if cursor else 0
    report["cursor_usage"] = report["model_usage"] if cursor else None
    report["receipt"] = receipt.build(report)
    if args.post:
        tracking = None
        if scope is not None and os.getenv("REVIEW_STATE_DIR") and state_dir().is_dir():
            tracking = (state_dir(), report["source"], report["head"], report["findings"], *scope)
            track(report, tracking, persist=False)
        try:
            publish(args.change, report["head"], report, project=getattr(args, "forge_project", None),
                    state=getattr(args, "forge_state", None))
        except (ReviewError, OSError, ValueError, subprocess.TimeoutExpired) as err:
            report.pop("outcomes", None)
            return dict(report, publication="failed", publication_error=str(err) or type(err).__name__), 2
        if tracking is not None:
            track(report, tracking, persist=True)
        report["publication"] = "posted-or-already-present"
    return report, code


def main():
    args = parser().parse_args()
    args.repo = args.repo.resolve()
    args.cache_dir = args.cache_dir.resolve()
    if args.go_report:
        args.go_report = args.go_report.resolve()
    if args.qualgen_store:
        args.qualgen_store = args.qualgen_store.resolve()
    old = Path.cwd()
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
                args.forge_project = project
                args.forge_state = state
                args.managed_project = project if project.managed else None
            # The forge and desk tools discover their configured forge from the caller's checkout.
            os.chdir(args.repo)
            report, status = run_request(args)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return status
    except (ReviewError, OSError, ValueError, TypeError, KeyError, AttributeError,
            UnidiffParseError, yaml.YAMLError, subprocess.TimeoutExpired) as err:
        print(f"cursor-review: {err}", file=sys.stderr)
        return 2
    finally:
        os.chdir(old)


if __name__ == "__main__":
    sys.exit(main())
