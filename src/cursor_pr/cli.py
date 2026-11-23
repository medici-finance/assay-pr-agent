import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from unidiff.errors import UnidiffParseError
import yaml

from .setup import apply_saved_setup, state_dir
from .config import load_config
from .credentials import forge_credentials, standalone_requested
from .checks import go_report, scan_files
from .engine import CursorFailure, pack, review, tokens
from .source import ReviewError, ci_refs, forge_change, local_change, run_desk


def publish(address, head, report):
    repo, number = address.rsplit("#", 1)
    body = [f"Cursor review of {head}", report["state"]]
    for issue in report["findings"]:
        body.append(f"- {issue['path']}:{issue['line']} — {issue['title']}. {issue['message']}")
    if report["state"] == "over-budget":
        body.append(f"Skipped Cursor. Estimated prompt {report['estimated_input_tokens']} tokens "
                    f"exceeds the limit of {report['max_input_tokens']}.")
    elif report["state"] == "model-unavailable":
        body.append("Cursor could not complete the review.")
    elif not report["findings"]:
        body.append("No model findings in the reviewed scope.")
    for check in report["checks"]:
        if check["state"] != "complete":
            body.append(f"- {check['tool']}: could not check {check.get('path', '')}")
        for item in check["findings"]:
            body.append(f"- {item['path']}:{item['line']} — {item['rule']} {item.get('message', '')}")
    for path in report["omitted"]:
        body.append(f"- Outside review coverage: {path}")
    body.extend(["", "Review tooling built on [PR-Agent](https://github.com/the-pr-agent/pr-agent)."])
    with tempfile.NamedTemporaryFile("w", encoding="utf8") as tmp:
        # A finding may quote a multiline source fragment, including forge quick actions.
        tmp.write("\n".join(" ".join(line.splitlines()) for line in body) + "\n")
        tmp.flush()
        run_desk(["deskpost", "comment", repo, number, "--kind", "mr", "--head", head,
             "--body-file", tmp.name])


def parser():
    p = argparse.ArgumentParser(description="Run one bounded Cursor review and emit a JSON report.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--change", help="Assay-configured GitHub PR or GitLab MR, e.g. 'org/repo#42'")
    mode.add_argument("--base", help="Local base revision to compare against")
    mode.add_argument("--ci", action="store_true", help="Use provider CI event SHAs in the checkout")
    p.add_argument("--expected-change-url", help="Require this exact open forge change before reviewing")
    p.add_argument("--head", help="Local head revision (default HEAD with --base)")
    p.add_argument("--repo", type=Path, default=Path.cwd(), help="Checkout for local Git and scanners")
    p.add_argument("--path", action="append", default=[], help="Limit review to an exact changed path; repeatable")
    p.add_argument("--go-report", type=Path, help="Assay go-analysis/v1 JSON for this head")
    p.add_argument("--model", help="Explicit Cursor model (otherwise the CLI default)")
    p.add_argument("--max-input-tokens", type=int, help="Preflight estimate limit (default 6000)")
    p.add_argument("--estimate", action="store_true", help="Prepare the inputs and report cost estimate without calling Cursor")
    p.add_argument("--cache-dir", type=Path, default=Path(os.getenv("XDG_CACHE_HOME", Path.home()/".cache"))/"cursor-pr-review")
    p.add_argument("--post", action="store_true", help="Publish a head-bound summary using deskpost with --change")
    return p


def execute(args):
    if args.max_input_tokens < 1 or (args.head and not args.base) or (args.post and (not args.change or args.estimate)):
        raise ReviewError("Invalid budget or incompatible --head/--post options")
    root = args.repo.resolve()
    if args.expected_change_url and not args.change:
        raise ReviewError("--expected-change-url requires --change")
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
    checks = scan_files(root, change.head, files)
    if args.go_report:
        go = go_report(args.go_report, change.head)
        go["findings"] = [row for row in go["findings"] if row["path"] in selected]
        checks.append(go)

    prompt, anchors, unseen, has_text = pack(files, checks)
    estimated = tokens(prompt) if has_text else 0
    omitted = sorted(set(unseen) | (paths - selected))
    report = {"source": change.identity, "head": change.head, "model": args.model or "cursor-default",
              "estimated_input_tokens": estimated, "max_input_tokens": args.max_input_tokens,
              "cursor_invocations": 0, "cache_hit": False, "omitted": omitted, "checks": checks,
              "findings": [], "cursor_usage": None}
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
    incomplete = bool(omitted or any(c["state"] != "complete" for c in checks))
    report["state"] = "partial" if incomplete else ("reviewed" if has_text else "empty")
    return report, 2 if incomplete else 0


def run_request(args):
    report, code = execute(args)
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
    old = Path.cwd()
    try:
        with ExitStack() as stack:
            state = state_dir().resolve()
            if args.change and os.getenv("CURSOR_REVIEW_POLLER_CHILD") != "1":
                apply_saved_setup(state)
            if args.max_input_tokens is None:
                args.max_input_tokens = int(os.getenv("REVIEW_MAX_INPUT_TOKENS", "6000"))
            if args.model is None:
                args.model = os.getenv("REVIEW_MODEL")
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
