import argparse
import fcntl
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from unidiff.errors import UnidiffParseError
import yaml

from . import history, providers, scanners
from .scanners import fence
from .login import auth_mode, login_directory
from .setup import locked, apply_saved_setup, load_provider_key, state_dir
from .config import load_config, project_from
from .credentials import forge_credentials, standalone_requested
from .artifacts import forge_go_report
from .checks import go_report, scan_files, scanner
from .sync import scan_checkout
from .engine import CursorFailure, pack, review, tokens
from .source import ReviewError, ci_refs, forge_change, local_change, run_desk


def publish(address, head, report):
    repo, number = address.rsplit("#", 1)
    label = providers.LABELS[report.get("provider", "cursor")]
    body = [f"{label} review of {head}", report["state"]]
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
    for path in report["omitted"]:
        body.append(f"- Outside review coverage: {fence(path)}")
    body.extend(["", "Review tooling built on [PR-Agent](https://github.com/the-pr-agent/pr-agent)."])
    with tempfile.NamedTemporaryFile("w", encoding="utf8") as tmp:
        # A finding may quote a multiline source fragment, including forge quick actions.
        tmp.write("\n".join(" ".join(line.splitlines()) for line in body) + "\n")
        tmp.flush()
        run_desk(["deskpost", "comment", repo, number, "--kind", "mr", "--head", head,
             "--body-file", tmp.name])


def parser():
    p = argparse.ArgumentParser(description="Run one bounded code review and emit a JSON report.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--change", help="Assay-configured GitHub PR or GitLab MR, e.g. 'org/repo#42'")
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
    p.add_argument("--max-input-tokens", type=int, help="Preflight estimate limit (default 6000)")
    p.add_argument("--estimate", action="store_true", help="Prepare the inputs and report cost estimate without calling the model")
    p.add_argument("--cache-dir", type=Path, default=Path(os.getenv("XDG_CACHE_HOME", Path.home()/".cache"))/"cursor-pr-review")
    p.add_argument("--post", action="store_true", help="Publish a head-bound summary using deskpost with --change")
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
        change = forge_change(args.change, args.expected_change_url)
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
    needs_checkout = missing or any(scanner(f.path) and not f.absent and f.status not in ("removed", "deleted", "D")
                                    for f in files)
    with ExitStack() as stack:
        checkout = root
        if project and needs_checkout:
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
    go = None
    if args.go_report:
        go = go_report(args.go_report, change.head)
    elif args.go_analysis_producer:
        go = forge_go_report(change.identity, args.go_analysis_producer, change.head, root)
    if go is not None:
        go["findings"] = [row for row in go["findings"] if row["path"] in selected]
        checks.append(go)

    prompt, anchors, unseen, has_text = pack(files, checks)
    estimated = tokens(prompt) if has_text else 0
    omitted = sorted(set(unseen) | (paths - selected))
    backend = providers.provider()
    report = {"provider": backend, "source": change.identity, "head": change.head, "model": args.model or f"{backend}-default",
              "estimated_input_tokens": estimated, "max_input_tokens": args.max_input_tokens,
              "cursor_invocations": 0, "cache_hit": False, "omitted": omitted, "checks": checks,
              "findings": [], "cursor_usage": None}
    if static:
        report["static_checks"] = [static[key] for key in sorted(static)]
    if backend == "cursor" and os.getenv("REVIEW_CURSOR_TEAM_ID"):
        report["cursor_team_id"] = os.environ["REVIEW_CURSOR_TEAM_ID"]
    if args.qualgen_store:
        report["qualgen"] = history.screen(root, args.qualgen_store, selected, change.head)
    if estimated > args.max_input_tokens:
        return dict(report, state="over-budget"), 2
    if args.estimate:
        return dict(report, state="estimate"), 0
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
    report["state"] = "partial" if incomplete else ("reviewed" if has_text else "empty")
    return report, 2 if incomplete else 0


def run_request(args):
    report, code = execute(args)
    backend = report["provider"]
    if backend != "cursor":
        report["model_invocations"] = report["cursor_invocations"]
        report["model_usage"] = report["cursor_usage"]
        report.update(cursor_invocations=0, cursor_usage=None)
    else:
        report.update(model_invocations=report["cursor_invocations"], model_usage=report["cursor_usage"])
    if args.post:
        try:
            publish(args.change, report["head"], report)
        except (ReviewError, OSError, subprocess.TimeoutExpired):
            return dict(report, publication="failed"), 2
        report["publication"] = "posted-or-already-present"
    return report, code


def main():
    args = parser().parse_args()
    # Load PR-Agent settings only from an empty directory, independent of the target repo.
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
                args.max_input_tokens = int(os.getenv("REVIEW_MAX_INPUT_TOKENS", "6000"))
            if args.model is None:
                args.model = os.getenv("REVIEW_MODEL")
            if args.change and os.getenv("CURSOR_REVIEW_POLLER_CHILD") == "1" and os.getenv("REVIEW_MANAGED_PROJECT"):
                args.managed_project = project_from(json.loads(os.environ["REVIEW_MANAGED_PROJECT"]))
            if args.change and standalone_requested():
                state.mkdir(mode=0o700, parents=True, exist_ok=True)
                projects = load_config(state)
                slug, _, number = args.change.rpartition("#")
                project = next((p for p in projects.values() if p.slug == slug), None)
                if project is None or not number.isdecimal():
                    raise ReviewError("Use a --change address from the configured review projects")
                if args.expected_change_url and args.expected_change_url != project.change_url(number):
                    raise ReviewError("Configured project and expected change URL differ")
                args.expected_change_url = project.change_url(number)
                if args.scanner_ci is None:
                    args.scanner_ci = project.scanner_ci
                args.managed_project = project if project.managed else None
                stack.enter_context(forge_credentials(projects, state))
            clean = stack.enter_context(tempfile.TemporaryDirectory(prefix="pr-agent-settings-"))
            # deskread/deskpost discover their configured forge from the caller's checkout.
            # Compute the upstream imports here, then restore cwd before touching the forge.
            os.chdir(clean)
            from pr_agent.algo.git_patch_processing import to_hunk_only_patch  # noqa: F401
            from pr_agent.algo.token_handler import TokenEncoder  # noqa: F401
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
