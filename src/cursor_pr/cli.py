import argparse
import fcntl
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from unidiff.errors import UnidiffParseError
import yaml

from . import context, drift, history, providers, scanners
from .scanners import fence
from .login import auth_mode, login_directory
from .setup import locked, apply_saved_setup, load_provider_key, max_input_tokens, state_dir
from .config import DEFAULT_MAX_INPUT_TOKENS, load_config, project_from
from .credentials import forge_credentials
from .artifacts import forge_go_report
from .checks import go_report, scan_files
from .sync import scan_checkout
from .discovery import comment_threads
from .engine import MAX_FINDINGS, CursorFailure, kept, pack, rank, review, section, tokens, verification
from .source import ReviewError, ci_refs, forge_change, local_change, post_comment


MAX_DEGRADED_ROWS = 50
# Each tier's whole budget is this multiple of the configured input-token limit.
TIERS = {"regular": 1, "mid": 2, "max": 10}
CAPS = {"regular": MAX_FINDINGS, "mid": 6, "max": 10}
TIMEOUTS = {"regular": 120, "mid": 240, "max": 480}
DEEP_FOCUS = ("Look for what only this wider view shows: a caller the change breaks, behaviour the existing "
              "tests do not cover, and a description or earlier comment the change contradicts or leaves unanswered.")
USED = ("included", "truncated")


def fit(files, checks, limit, cap=MAX_FINDINGS):
    """Pack the prompt, leaving out whole files until it fits the input budget.

    Returns the prompt parts, the estimate of what will be sent, the estimate of the full
    prompt, and the paths left out. Nothing is left out when even one file cannot fit.
    """
    prompt, anchors, unseen, has_text = full_pack = pack(files, checks, max_findings=cap)
    estimated = full = tokens(prompt) if has_text else 0
    if full <= limit:
        return full_pack, full, full, []
    sizes = {f.path: tokens(text) for f in files if (text := section(f)[0])}
    # A file that cannot fit even alone leaves first, so it never takes a file that fits with it.
    overhead = tokens(pack(files, checks, frozenset(sizes), max_findings=cap)[0])
    queue = sorted(sizes, key=lambda path: (overhead + sizes[path] <= limit, rank(path), -sizes[path], path))
    dropped = []
    while estimated > limit and queue:
        # Per-file sizes are approximate, so the last file only leaves after a real recount.
        dropped.append(queue.pop(0))
        freed = sizes[dropped[-1]]
        while len(queue) > 1 and freed < estimated - limit:
            dropped.append(queue.pop(0))
            freed += sizes[dropped[-1]]
        prompt, anchors, unseen, has_text = pack(files, checks, frozenset(dropped), max_findings=cap)
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
                    f"over the limit of {report.get('prompt_limit', report['max_input_tokens'])}. "
                    f"{label} did not review these files:")
        body.extend(f"- Not reviewed (token limit): {fence(path)}" for path in degraded[:MAX_DEGRADED_ROWS])
        if len(degraded) > MAX_DEGRADED_ROWS:
            body.append(f"- Not reviewed (token limit): {len(degraded) - MAX_DEGRADED_ROWS} more files")
    tier = report.get("tier", "regular")
    if tier != "regular":
        body.append(f"Tier {tier}: budget {report['max_input_tokens']} tokens ({TIERS[tier]}× the configured "
                    f"{report['base_input_tokens']}), estimated use {report['estimated_input_tokens']}.")
        if report["delivered"] != tier:
            body.append(f"Degraded review: {tier} was requested and {report['delivered']} was delivered.")
        blocks = {row["name"]: row["state"] for row in report["context"]}
        used = [name + (" (truncated)" if state == "truncated" else "") for name, state in blocks.items() if state in USED]
        body.append("Context beyond the patch: " + (", ".join(used) or "none") + ".")
        missing = [f"{name} ({state})" for name, state in blocks.items() if state not in USED and state != "empty"]
        if missing:
            body.append("Not included: " + ", ".join(missing) + ".")
        for row in report["passes"]:
            if row["state"] == "failed":
                body.append(f"The {row['name']} pass did not complete.")
        check = report["verification"]
        if check["state"] == "unavailable":
            body.append("The findings could not be verified by a second pass.")
        elif check["state"] == "filtered":
            body.append(f"A verification pass kept {check['after']} of {check['before']} findings.")
    # Paths come from the PR and titles/messages from the model: quote them as code
    # so the reviewer identity never posts a Markdown link or @mention for an attacker.
    for issue in report["findings"]:
        body.append(f"- {fence(issue['path'])}:{issue['line']} — {fence(issue['title'])}. {fence(issue['message'])}")
    if report["state"] == "over-budget":
        body.append(f"Skipped {label}. Estimated prompt {report['estimated_input_tokens']} tokens "
                    f"exceeds the limit of {report.get('prompt_limit', report['max_input_tokens'])}.")
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
    p.add_argument("--tier", choices=list(TIERS), default="regular",
                   help="Review depth: mid and max add context beyond the patch at 2 and 10 times the token limit")
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
        notes = None
        if args.tier == "max" and args.change:
            number = int(args.change.rsplit("#", 1)[1])
            try:
                notes = comment_threads(args.forge_project, [number]).get(number)
            except (ReviewError, OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired):
                notes = None
        raw = context.gather(args.tier, root, checkout, change, files, drifted, notes)
    go = None
    if args.go_report:
        go = go_report(args.go_report, change.head)
    elif args.go_analysis_producer:
        go = forge_go_report(change.identity, args.go_analysis_producer, change.head, root)
    if go is not None:
        go["findings"] = [row for row in go["findings"] if row["path"] in selected]
        checks.append(go)

    tier, base = args.tier, args.max_input_tokens
    budget = base * TIERS[tier]
    backend = providers.provider()
    # Mid and max send the same first prompt, so a max review reuses a cached mid result.
    reserve = 0 if tier == "regular" else base // (2 if tier == "max" else 4)
    limit = base if tier == "regular" else 2 * base - base // 4
    cap = CAPS["regular" if tier == "regular" else "mid"]
    (prompt, anchors, unseen, has_text), estimated, full, degraded = fit(files, checks, limit, cap)
    skip = frozenset(degraded)
    fits = has_text and estimated <= limit
    fates, deep, deep_fates = [], None, []
    if tier != "regular" and fits:
        patch_tokens = estimated
        text, fates = context.assemble(context.MID, raw, limit - patch_tokens - 80)
        wider = pack(files, checks, skip, context=text, max_findings=cap)[0] if text else prompt
        if tokens(wider) <= limit:
            prompt, estimated = wider, tokens(wider)
        else:
            fates = [dict(row, state="dropped") if row["state"] in USED else row for row in fates]
        if tier == "max":
            deep_limit = budget - estimated - reserve
            text, deep_fates = context.assemble(context.DEEP, raw, deep_limit - patch_tokens - 120)
            if text:
                wider = pack(files, checks, skip, context=text, max_findings=cap, focus=DEEP_FOCUS)[0]
                if tokens(wider) <= deep_limit:
                    deep = wider
                else:
                    deep_fates = [dict(row, state="dropped") if row["state"] in USED else row for row in deep_fates]
    passes = [{"name": "review", "estimated_input_tokens": estimated, "state": "planned"}]
    if deep:
        passes.append({"name": "deep", "estimated_input_tokens": tokens(deep), "state": "planned"})
    omitted = sorted(set(unseen) | (paths - selected))
    report = {"provider": backend, "source": change.identity, "head": change.head, "model": args.model or f"{backend}-default",
              "estimated_input_tokens": sum(row["estimated_input_tokens"] for row in passes),
              "max_input_tokens": budget, "base_input_tokens": base, "prompt_limit": limit, "tier": tier,
              "cursor_invocations": 0, "cache_hit": False, "omitted": omitted, "checks": checks,
              "findings": [], "cursor_usage": None, "full_input_tokens": full, "degraded": degraded,
              "drift": drifted, "passes": passes, "context": fates + [row for row in deep_fates
                                                                      if row["name"] not in context.MID],
              "verification": {"state": "not-needed"}, "delivered": "regular"}
    if static:
        report["static_checks"] = [static[key] for key in sorted(static)]
    if backend == "cursor" and os.getenv("REVIEW_CURSOR_TEAM_ID"):
        report["cursor_team_id"] = os.environ["REVIEW_CURSOR_TEAM_ID"]
    if args.qualgen_store:
        report["qualgen"] = history.screen(root, args.qualgen_store, selected, change.head)
    if estimated > limit:
        return dict(report, state="over-budget"), 2
    if any(row["state"] in USED for row in fates):
        report["delivered"] = "mid"
    if deep and any(row["state"] in USED for row in deep_fates):
        report["delivered"] = "max"
    lowered = report["delivered"] != tier
    if args.estimate:
        return dict(report, state="estimate"), 2 if degraded or lowered else 0
    if has_text:
        calls = []
        try:
            answer, cached = review(prompt, args.model, anchors, args.cache_dir, change.identity,
                                    limit=cap, timeout=TIMEOUTS["regular" if tier == "regular" else "mid"])
        except CursorFailure as err:
            return dict(report, state="model-unavailable", error=str(err),
                        cursor_invocations=err.invocations, cursor_usage=err.usage), 2
        except (ReviewError, OSError, ValueError, KeyError, TypeError, AttributeError,
                subprocess.TimeoutExpired, yaml.YAMLError) as err:
            # Preserve deterministic diagnostics even if the model failed.
            return dict(report, state="model-unavailable", error=str(err) if isinstance(err, ReviewError)
                        else type(err).__name__, cursor_invocations=None), 2
        calls.append(cached)
        passes[0]["state"] = "cached" if cached else "called"
        findings, usage = answer["findings"], None if cached else answer.get("cursor_usage")
        if deep:
            try:
                more, cached = review(deep, args.model, anchors, args.cache_dir, change.identity,
                                      limit=cap, timeout=TIMEOUTS["max"])
                calls.append(cached)
                passes[1]["state"] = "cached" if cached else "called"
                seen = {(f["path"], f["line"], f["end_line"]) for f in findings}
                findings = findings + [f for f in more["findings"] if (f["path"], f["line"], f["end_line"]) not in seen]
            except (ReviewError, OSError, ValueError, KeyError, TypeError, AttributeError,
                    subprocess.TimeoutExpired, yaml.YAMLError) as err:
                # The first pass stands on its own; say that the deeper one is missing.
                calls.append(not getattr(err, "invocations", 1))
                passes[1]["state"] = "failed"
                report["delivered"] = "mid" if any(row["state"] in USED for row in fates) else "regular"
        if tier != "regular" and findings:
            before = len(findings)
            check = next((text for radius in (15, 8, 3, 0)
                          if tokens(text := verification(findings, files, radius)) <= reserve), None)
            report["verification"] = {"state": "unavailable", "before": before}
            if check:
                passes.append({"name": "verification", "estimated_input_tokens": tokens(check), "state": "failed"})
                report["estimated_input_tokens"] += tokens(check)
                try:
                    verdict, cached = review(check, args.model, anchors, args.cache_dir, change.identity, limit=before)
                    calls.append(cached)
                    passes[-1]["state"] = "cached" if cached else "called"
                    findings = kept(findings, verdict)
                    report["verification"] = {"state": "filtered", "before": before, "after": len(findings)}
                except (ReviewError, OSError, ValueError, KeyError, TypeError, AttributeError,
                        subprocess.TimeoutExpired, yaml.YAMLError) as err:
                    calls.append(not getattr(err, "invocations", 1))
        report.update(findings=findings[:CAPS[tier]], cursor_usage=usage, cache_hit=all(calls),
                      cursor_invocations=sum(not cached for cached in calls))
    lowered = report["delivered"] != tier or report["verification"]["state"] == "unavailable"
    incomplete = bool(omitted or any(c["state"] != "complete" for c in [*checks, *static.values()]))
    if degraded or lowered:
        report["state"] = "degraded"
    else:
        report["state"] = "partial" if incomplete else ("reviewed" if has_text else "empty")
    return report, 2 if incomplete or degraded or lowered else 0


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
            publish(args.change, report["head"], report, project=getattr(args, "forge_project", None),
                    state=getattr(args, "forge_state", None))
        except (ReviewError, OSError, ValueError, subprocess.TimeoutExpired) as err:
            return dict(report, publication="failed", publication_error=str(err) or type(err).__name__), 2
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
            clean = stack.enter_context(tempfile.TemporaryDirectory(prefix="pr-agent-settings-"))
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
