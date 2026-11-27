import argparse
from collections import defaultdict
import fcntl
import hashlib
from contextlib import ExitStack, closing
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

import httpx
from unidiff.errors import UnidiffParseError
import yaml

from . import context, drift, engine, history, providers, sarif, scanners
from .scanners import fence
from .login import auth_mode, login_directory
from .setup import locked, apply_saved_setup, load_provider_key, max_findings, max_input_tokens, state_dir
from .config import DEFAULT_MAX_FINDINGS, DEFAULT_MAX_INPUT_TOKENS, FINDINGS_SETTINGS, MAX_FINDINGS_LIMIT, load_config, project_from
from .credentials import forge_credentials
from .artifacts import forge_go_report
from .checks import blob, go_report, scan_files
from .sync import scan_checkout
from .discovery import context_comments
from .engine import (MAX_FINDINGS, CursorFailure, merge_findings, pack, prompt_advisories, rank, render_sections,
                     review, tokens, verification, visible_advisories)
from .source import ReviewError, ci_refs, forge_change, local_change, post_comment, valid_sha


MAX_DEGRADED_ROWS = 50
FORGE_COMMENT_BYTES = 16 * 1024
COMPACT_FINDING_MESSAGE = 140
# Each tier's whole budget is this multiple of the configured input-token limit.
TIERS = {"regular": 1, "mid": 2, "max": 10}
TIMEOUTS = {"regular": 120, "mid": 240, "max": 480}
DEEP_FOCUS = ("Look for what only this wider view shows: a caller the change breaks, behaviour the existing "
              "tests do not cover, and earlier discussion that points to a concrete defect in the shown patch.")
USED = ("included", "truncated")


def add_history(files, checks, packed, estimate, limit, dropped, advisories, **options):
    """Fit a prefix of the notices without changing the chosen patch scope or tier context."""
    if estimate >= limit:
        return packed, estimate
    notices = visible_advisories(advisories, packed[1])
    skipped = frozenset(dropped)
    smallest, largest = 1, len(notices)
    while smallest <= largest:
        count = (smallest + largest) // 2
        candidate = pack(files, checks, skipped, advisories=notices[:count], **options)
        candidate_size = tokens(candidate[0])
        if candidate_size <= limit:
            packed, estimate = candidate, candidate_size
            smallest = count + 1
        else:
            largest = count - 1
    return packed, estimate


def fit(files, checks, limit, cap=MAX_FINDINGS, *, title="", description="", focus="", advisories=(), rendered=None,
        scoped=False, commit_messages=False, description_truncated=False):
    """Fill the prompt with reviewable patches first and use spare space for history."""
    if rendered is None:
        rendered = render_sections(files)
    options = dict(max_findings=cap, focus=focus, rendered=rendered, scoped=scoped,
                   commit_messages=commit_messages, description_truncated=description_truncated)
    if advisories:
        original = pack(files, checks, title=title, description=description, advisories=advisories, **options)
        original_size = tokens(original[0]) if original[3] else 0
        if original_size <= limit:
            return original, original_size, original_size, []

    packed, estimate, full, dropped, has_metadata = fit_patch(files, checks, limit, title, description, **options)
    if not advisories:
        return packed, estimate, full, dropped
    if estimate > limit:
        return original, original_size, original_size, []
    if not has_metadata:
        title = description = ""
    packed, estimate = add_history(files, checks, packed, estimate, limit, dropped, advisories,
                                   title=title, description=description, **options)
    return packed, estimate, original_size, dropped


def fit_patch(files, checks, limit, title, description, **options):
    """Pack the prompt, leaving out whole files until it fits the input budget.

    Returns the prompt, token estimates, skipped paths and whether author context was kept.
    """
    prompt, anchors, unseen, has_text = full_pack = pack(
        files, checks, title=title, description=description, **options)
    estimated = full = tokens(prompt) if has_text else 0
    if full <= limit:
        return full_pack, full, full, [], bool(title or description)
    if title or description:
        plain = pack(files, checks, **options)
        plain_estimate = tokens(plain[0])
        if plain_estimate <= limit:
            return plain, plain_estimate, full, [], False
    # Measure each file with its scanner evidence before choosing what to leave out.
    # A file that cannot fit alone leaves first, so a smaller file can still be reviewed.
    findings_by_path = defaultdict(list)
    for check in checks:
        for finding in check["findings"]:
            findings_by_path[finding["path"]].append(finding)
    singleton = dict(options, scoped=options["scoped"] or len(files) > 1)
    overhead = tokens(pack([], [], title=title, description=description, **singleton)[0])
    sizes = {}
    for file in files:
        if options["rendered"][file.path][0] is not None:
            file_checks = [{"findings": findings_by_path.get(file.path, ())}]
            standalone, _, _, _ = pack([file], file_checks, title=title, description=description, **singleton)
            sizes[file.path] = tokens(standalone) - overhead
    queue = sorted(sizes, key=lambda path: (overhead + sizes[path] <= limit, rank(path), -sizes[path], path))
    dropped = []
    batch_start = 0
    while estimated > limit and queue:
        # Per-file sizes are approximate, so the last file only leaves after a real recount.
        batch_start = len(dropped)
        dropped.append(queue.pop(0))
        freed = sizes[dropped[-1]]
        while len(queue) > 1 and freed < estimated - limit:
            dropped.append(queue.pop(0))
            freed += sizes[dropped[-1]]
        prompt, anchors, unseen, has_text = pack(files, checks, frozenset(dropped), title, description, **options)
        estimated = tokens(prompt) if has_text else 0
    # The final batch may have removed too much because tokenizer boundaries
    # differ between standalone patches and the combined prompt.
    smallest, largest = batch_start + 1, len(dropped)
    while estimated <= limit and smallest < largest:
        count = (smallest + largest) // 2
        candidate = pack(files, checks, frozenset(dropped[:count]), title, description, **options)
        candidate_size = tokens(candidate[0])
        if candidate_size <= limit:
            prompt, anchors, unseen, has_text = candidate
            estimated = candidate_size
            largest = count
        else:
            smallest = count + 1
    del dropped[largest:]
    fits = has_text and estimated <= limit
    if (title or description) and (not fits or len(dropped) > 1):
        fitted, estimate, _, skipped, _ = fit_patch(files, checks, limit, "", "", **options)
        if fitted[3] and estimate <= limit and (not fits or len(skipped) < len(dropped)):
            return fitted, estimate, full, skipped, False
    if not fits:
        return full_pack, full, full, [], bool(title or description)
    return (prompt, anchors, unseen, has_text), estimated, full, sorted(dropped), bool(title or description)


def shown_description(kind, fates):
    state = next((row["state"] for row in fates if row["name"] == "change description"), "empty")
    if state not in USED:
        return ""
    return "truncated" if kind == "author" and state == "truncated" else kind


def with_context(files, checks, packed, estimated, dropped, limit, cap, names, raw, *, rendered, focus="",
                 scoped=False, description_kind=""):
    """Fit tier-specific context to an already selected scope."""
    text, fates = context.assemble(names, raw, limit - estimated - (120 if focus else 80))
    options = dict(max_findings=cap, focus=focus, rendered=rendered, scoped=scoped)
    baseline = estimated
    if text:
        skipped = frozenset(dropped)
        wider = pack(files, checks, skipped, context=text, **options)
        size = tokens(wider[0])
        if size <= limit:
            packed, estimated, baseline = wider, size, size
            kind = shown_description(description_kind, fates)
            if kind:
                guided = pack(files, checks, skipped, context=text, context_description=kind, **options)
                guided_size = tokens(guided[0])
                if guided_size <= baseline:
                    packed, estimated = guided, guided_size
        else:
            fates = [dict(row, state="dropped") if row["state"] in USED else row for row in fates]
            text = ""
    return packed, estimated, fates, text, baseline


def run_pass(row, prompt, anchors, args, source, *, cap, timeout, originals=None):
    """Run one pass and record billing data only for fresh provider calls."""
    before = engine.started
    try:
        answer, cached = review(prompt, args.model, anchors, args.cache_dir, source,
                                limit=cap, timeout=timeout, originals=originals)
    except CursorFailure as err:
        row.update(state="failed", model_invocations=engine.started - before)
        if row["model_invocations"] and err.usage is not None:
            row["model_usage"] = err.usage
        return None, str(err)
    except (ReviewError, OSError, ValueError, KeyError, TypeError, AttributeError,
            subprocess.TimeoutExpired, yaml.YAMLError) as err:
        row.update(state="failed", model_invocations=engine.started - before)
        return None, str(err) if isinstance(err, ReviewError) else type(err).__name__
    row.update(state="cached" if cached else "called", model_invocations=engine.started - before)
    if not cached and answer.get("cursor_usage") is not None:
        row["model_usage"] = answer["cursor_usage"]
    return answer, None


def merged_fates(groups):
    """One row per context block, from a prompt that used it when any did."""
    rows = {}
    for fates in groups:
        for row in fates:
            if row["name"] not in rows or (row["state"] in USED and rows[row["name"]]["state"] not in USED):
                rows[row["name"]] = row
    return rows


def reached(prompts):
    """The tier whose context the (pass name, block fates) prompts contained, each tier holding the one below."""
    used = [(name, row["name"]) for name, fates in prompts for row in fates if row["state"] in USED]
    if not any(block in context.MID for _, block in used):
        return "regular"
    return "max" if any(name == "deep" and block not in context.MID for name, block in used) else "mid"


def total_usage(passes):
    """Preserve a single usage record and sum additive counters across fresh passes."""
    samples = [row["model_usage"] for row in passes if "model_usage" in row]
    if len(samples) <= 1:
        return next(iter(samples), None)
    totals = {}
    for sample in samples:
        for key, value in sample.items():
            if type(value) in (int, float):
                totals[key] = totals.get(key, 0) + value
    return totals or None


def finding_line(issue, *, compact=False, shorten_path=False, clip=None):
    # Fence displayed text after clipping so the shortened value stays literal.
    path = issue["path"]
    if shorten_path and len(path) > 80:
        tag = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
        path = f"…/{path[-80:]} [{tag}]"
    title = fence(issue["title"], 60 if compact else None)
    message = fence(issue["message"], COMPACT_FINDING_MESSAGE if compact else clip)
    return f"- {fence(path)}:{issue['line']} — {title}. {message}" + (" (shortened)" if clip else "")


def size_of(text):
    return len(text.encode("utf-8"))


def shortened_finding(issue, allowance):
    """The finding row with the longest message that fits, keeping at least the compact 140 characters."""
    low, high, best = COMPACT_FINDING_MESSAGE, len(issue["message"]), None
    while low <= high:
        middle = (low + high) // 2
        row = comment_text([finding_line(issue, clip=middle)])
        if size_of(row) <= allowance:
            best, low = row, middle + 1
        else:
            high = middle - 1
    return best


def bounded_findings(issues, space):
    """Reserve the minimum for each row before sharing space for additional detail."""
    full = [comment_text([finding_line(issue)]) for issue in issues]
    rows = full.copy()
    for index, issue in enumerate(issues):
        if len(issue["message"]) > COMPACT_FINDING_MESSAGE:
            shortened = comment_text([finding_line(issue, clip=COMPACT_FINDING_MESSAGE)])
            rows[index] = min((full[index], shortened), key=size_of)
    minimum_sizes = [size_of(row) for row in rows]
    remaining = space - sum(minimum_sizes)
    if remaining < 0:
        return None

    order = sorted(range(len(issues)), key=lambda index: size_of(full[index]))
    for position, index in enumerate(order):
        allowance = minimum_sizes[index] + remaining // (len(order) - position)
        if size_of(full[index]) <= allowance:
            rows[index] = full[index]
        else:
            rows[index] = shortened_finding(issues[index], allowance) or rows[index]
        remaining -= size_of(rows[index]) - minimum_sizes[index]
    return rows


def comment_text(rows):
    # A finding may quote multiline source, including forge quick actions.
    return "\n".join(" ".join(line.splitlines()) for line in rows) + "\n"


def omission_notice(count, label):
    return f"{count} {label} omitted from this comment.\n" if count else ""


def fit_comment_rows(rows, allowance, label, *, already_omitted=0):
    """Fit whole rows and count entries that could not be shown."""
    rendered = [comment_text([row]) for row in rows]
    sizes = [len(row.encode("utf-8")) for row in rendered]
    notice = omission_notice(already_omitted, label)
    if sum(sizes) + len(notice.encode("utf-8")) <= allowance:
        return "".join(rendered) + notice
    omitted = already_omitted + len(rows)
    remaining = allowance - len(omission_notice(omitted, label).encode("utf-8"))
    included = []
    for row, size in zip(rendered, sizes, strict=True):
        if size <= remaining:
            included.append(row)
            remaining -= size
            omitted -= 1
    return "".join(included) + omission_notice(omitted, label)


def fit_tier_comment(header, findings, coverage, extra_coverage, outside, details, footer):
    """Keep finding summaries and coverage before spending space on optional evidence."""
    body = comment_text(header + ["Some review details were shortened to fit the forge comment."])
    end = comment_text(footer)
    space = FORGE_COMMENT_BYTES - len((body + end).encode("utf-8"))

    exact = [comment_text([finding_line(issue, compact=True)]) for issue in findings]
    abbreviated = [comment_text([finding_line(issue, compact=True, shorten_path=True)]) for issue in findings]
    model = [min((full, short), key=lambda row: len(row.encode("utf-8")))
             for full, short in zip(exact, abbreviated, strict=True)]
    space -= sum(len(row.encode("utf-8")) for row in model)

    evidence_label = "check and history rows"
    outside_label = "files outside review coverage"
    evidence_notice = len(omission_notice(len(details), evidence_label).encode("utf-8"))
    outside_notice = len(omission_notice(len(outside), outside_label).encode("utf-8"))
    skipped = fit_comment_rows(coverage, space - evidence_notice - outside_notice,
                               "files skipped by the token limit", already_omitted=extra_coverage)
    space -= len(skipped.encode("utf-8"))
    outside_text = fit_comment_rows(outside, space - evidence_notice, outside_label)
    space -= len(outside_text.encode("utf-8"))

    for index, full in enumerate(exact):
        cost = len(full.encode("utf-8")) - len(model[index].encode("utf-8"))
        if cost <= space - evidence_notice:
            model[index] = full
            space -= cost
    evidence = fit_comment_rows(details, space, evidence_label)
    return body + "".join(model) + skipped + outside_text + evidence + end


def publish(address, head, report, *, project=None, state=None):
    _, number = address.rsplit("#", 1)
    label = providers.LABELS[report.get("provider", "cursor")]
    body = [f"{label} review of {head}", report["state"]]
    degraded = report.get("degraded") or []
    coverage = []
    extra_coverage = max(0, len(degraded) - MAX_DEGRADED_ROWS)
    if degraded:
        body.append(f"Degraded review: the full prompt is an estimated {report['full_input_tokens']} tokens, "
                    f"over the limit of {report.get('prompt_limit', report['max_input_tokens'])}. "
                    f"{label} did not review these files:")
        coverage.extend(f"- Not reviewed (token limit): {fence(path)}" for path in degraded[:MAX_DEGRADED_ROWS])

    intro, body = body, []
    tier = report.get("tier", "regular")
    if tier != "regular" and report.get("delivered") is None:
        # Nothing reached the model: the state line below says why, and no tier or context was delivered.
        body.append(f"Tier {tier}: budget {report['max_input_tokens']} tokens ({TIERS[tier]}× the configured "
                    f"{report['base_input_tokens']}).")
    elif tier != "regular":
        body.append(f"Tier {tier}: budget {report['max_input_tokens']} tokens ({TIERS[tier]}× the configured "
                    f"{report['base_input_tokens']}), estimated use {report['estimated_input_tokens']}.")
        if report["delivered"] is not None and report["delivered"] != tier:
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
        if omitted_findings := report.get("omitted_findings"):
            noun = "finding" if omitted_findings == 1 else "findings"
            # A prepared report from an older worker recorded the omitted count and the capped list.
            finding_cap = report.get("max_findings", len(report["findings"]))
            body.append(f"{omitted_findings} {noun} omitted by the {finding_cap}-finding tier limit.")
    # Paths come from the PR and titles/messages from the model: quote them as code
    # so the reviewer identity never posts a Markdown link or @mention for an attacker.
    header, body = body, []
    findings = [finding_line(issue) for issue in report["findings"]]
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
    outside = [f"- Outside review coverage: {fence(path)}" for path in report["omitted"]]
    extra = [f"- Not reviewed (token limit): {extra_coverage} more files"] if extra_coverage else []
    footer = ["", "Review tooling built on [PR-Agent](https://github.com/the-pr-agent/pr-agent)."]
    text = comment_text(intro + coverage + extra + header + findings + body + outside + footer)
    # The forge helper refuses a body over 16 KiB: shorten rather than lose the whole comment.
    if tier == "regular" and findings and size_of(text) > FORGE_COMMENT_BYTES:
        # Keep every other row whole and shorten the longest finding messages first.
        before = comment_text(intro + coverage + extra + header + ["Some findings were shortened to fit the forge comment."])
        after = comment_text(body + outside + footer)
        rows = bounded_findings(report["findings"], FORGE_COMMENT_BYTES - size_of(before + after))
        if rows is not None:
            text = before + "".join(rows) + after
    if size_of(text) > FORGE_COMMENT_BYTES:
        text = fit_tier_comment(intro + header, report["findings"], coverage, extra_coverage, outside, body, footer)
        if size_of(text) > FORGE_COMMENT_BYTES:
            raise ReviewError("Review exceeded the forge comment limit")
    post_comment(project, int(number), head, text, state if state is not None else state_dir())


def findings_option(value):
    """A findings cap as the environment accepts it: int() syntax within the bound."""
    number = int(value)
    if not 1 <= number <= MAX_FINDINGS_LIMIT:
        raise ValueError(f"findings cap must be from 1 to {MAX_FINDINGS_LIMIT}")
    return number


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
    p.add_argument("--max-findings", type=findings_option,
                   help=f"Findings cap of the requested tier, 1 to {MAX_FINDINGS_LIMIT} (default {DEFAULT_MAX_FINDINGS}, or the "
                        "saved " + ", ".join(FINDINGS_SETTINGS.values()) + " setting for that tier)")
    p.add_argument("--tier", choices=list(TIERS), default="regular",
                   help="Review depth: mid and max add context beyond the patch at 2 and 10 times the token limit")
    p.add_argument("--estimate", action="store_true", help="Prepare the inputs and report cost estimate without calling the model")
    p.add_argument("--cache-dir", type=Path, default=Path(os.getenv("XDG_CACHE_HOME", Path.home()/".cache"))/"cursor-pr-review")
    p.add_argument("--post", action="store_true", help="Publish a head-bound summary with --change")
    p.add_argument("--sarif", type=Path, help="Also write the findings as a SARIF 2.1.0 log at this path")
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
        change = forge_change(args.change, args.expected_change_url, project=getattr(args, "forge_project", None),
                              with_description=True)
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

    caps = max_findings(state_dir().resolve(), tier=args.tier, override=args.max_findings)
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
            resources = getattr(args, "sarif_resources", stack)
            checkout = resources.enter_context(scan_checkout(root, project, number, change.head))
            if resources is not stack:
                args.sarif_checkout = checkout
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
        notes = None
        if args.tier == "max" and args.change:
            number = int(args.change.rsplit("#", 1)[1])
            try:
                notes = context_comments(args.forge_project, number)
            except (ReviewError, OSError, ValueError, KeyError, TypeError, AttributeError,
                    subprocess.TimeoutExpired, httpx.HTTPError):
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

    qualgen = history.screen(root, args.qualgen_store, selected, change.head) if args.qualgen_store else None
    advisories = qualgen["advisories"] if qualgen else ()

    tier, base = args.tier, args.max_input_tokens
    budget = base * TIERS[tier]
    backend = providers.provider()
    # Mid and max send the same first prompt, so a max review reuses a cached mid result.
    limit = base if tier == "regular" else 2 * base - base // 4
    first_tier = "regular" if tier == "regular" else "mid"
    cap = caps[first_tier]
    rendered = render_sections(files)
    scoped = selected != paths
    if not args.change or not (change.title or change.description):
        description_kind = "history"
    elif change.description_truncated:
        description_kind = "truncated"
    else:
        description_kind = "author" if change.description else "title"
    regular_options = {}
    if tier == "regular":
        regular_options = dict(title=change.title, description=change.description, advisories=advisories,
                               commit_messages=not args.change, description_truncated=change.description_truncated)
    first_pack, estimated, full, dropped = fit(files, checks, limit, cap, rendered=rendered, scoped=scoped, **regular_options)
    prompt, anchors, unseen, has_text = first_pack
    plans = []
    fates = context.assemble(context.MID, raw, 0)[1] if tier != "regular" else []
    deep_fates = context.assemble(context.DEEP, raw, 0)[1] if tier == "max" else []
    first_paths = set()
    if has_text and estimated <= limit:
        if tier != "regular":
            first_pack, estimated, fates, first_context, first_baseline = with_context(
                files, checks, first_pack, estimated, dropped, limit, cap, context.MID, raw,
                rendered=rendered, scoped=scoped, description_kind=description_kind)
            prompt = first_pack[0]
        plans.append(({"name": "review", "estimated_input_tokens": estimated, "state": "planned"}, prompt, anchors, fates))
        first_paths = {path for path, lines in anchors.items() if lines}

    deep_limit = limit
    if tier == "max" and has_text:
        # The reserve is for planning. Verification may use any tokens left after actual prompts.
        deep_limit = budget - (first_baseline if plans else 0) - base // 2
        deep_pack, deep_size, _, deep_dropped = fit(
            files, checks, deep_limit, cap, focus=DEEP_FOCUS, rendered=rendered, scoped=scoped)
        deep_anchors = deep_pack[1]
        if deep_size <= deep_limit:
            deep_pack, deep_size, deep_fates, deep_context, _ = with_context(
                files, checks, deep_pack, deep_size, deep_dropped, deep_limit, cap, context.DEEP, raw,
                rendered=rendered, focus=DEEP_FOCUS, scoped=scoped, description_kind=description_kind)
            new_paths = any(lines and path not in first_paths for path, lines in deep_anchors.items())
            extra_context = any(row["name"] not in context.MID and row["state"] in USED for row in deep_fates)
            if new_paths or extra_context:
                plans.append(({"name": "deep", "estimated_input_tokens": deep_size, "state": "planned"},
                              deep_pack[0], deep_anchors, deep_fates))
            else:
                deep_fates = [dict(row, state="dropped") if row["state"] in USED else row for row in deep_fates]

    if tier != "regular" and advisories:
        # Share the existing context allowance. The original pass sizes bound
        # history-bearing prompts, preserving the patch scope and verification budget.
        history_name = "history advisories (untrusted data)"
        for index, (row, _, locations, blocks) in enumerate(plans):
            is_deep = row["name"] == "deep"
            names, skipped, original_context = ((context.DEEP, deep_dropped, deep_context) if is_deep
                                                 else (context.MID, dropped, first_context))
            notices = prompt_advisories(advisories, locations)
            if not original_context or not notices:
                continue
            history_text = "\n".join(json.dumps(notice, ensure_ascii=False) for notice in notices)
            wider, fates_with_history = context.assemble(
                (*names, history_name), {**raw, history_name: history_text}, tokens(original_context))
            replacements = fates_with_history[:-1]
            previous_kind = shown_description(description_kind, blocks)
            replacement_kind = shown_description(description_kind, replacements)
            if previous_kind == "author" and replacement_kind != "author":
                continue
            if fates_with_history[-1]["state"] not in USED or any(
                    old["state"] in USED and new["state"] not in USED
                    for old, new in zip(blocks, replacements, strict=True)):
                continue
            candidate = pack(files, checks, frozenset(skipped), context=wider, max_findings=cap,
                             focus=DEEP_FOCUS if is_deep else "", rendered=rendered, scoped=scoped,
                             context_description=replacement_kind)[0]
            size = tokens(candidate)
            if size <= row["estimated_input_tokens"]:
                row["estimated_input_tokens"] = size
                blocks[:] = replacements
                plans[index] = row, candidate, locations, blocks

    passes = [row for row, _, _, _ in plans]
    context_fates = merged_fates([fates, deep_fates])
    planned_tier = reached([(row["name"], blocks) for row, _, _, blocks in plans]) if plans else None
    reviewable = selected - set(unseen)
    planned_paths = {path for _, _, locations, _ in plans for path, lines in locations.items() if lines}
    degraded = sorted(reviewable - planned_paths) if plans else []
    omitted = sorted(set(unseen) | (paths - selected))
    report = {"provider": backend, "source": change.identity, "head": change.head, "model": args.model or f"{backend}-default",
              "estimated_input_tokens": sum(row["estimated_input_tokens"] for row in passes),
              "max_input_tokens": budget, "base_input_tokens": base, "prompt_limit": limit, "tier": tier,
              "cursor_invocations": 0, "cache_hit": False, "omitted": omitted, "checks": checks,
              "findings": [], "cursor_usage": None, "full_input_tokens": full, "degraded": degraded,
              "drift": drifted, "passes": passes, "context": list(context_fates.values()),
              "verification": {"state": "not-needed"}, "delivered": planned_tier, "max_findings": caps[tier]}
    if static:
        report["static_checks"] = [static[key] for key in sorted(static)]
    if backend == "cursor" and os.getenv("REVIEW_CURSOR_TEAM_ID"):
        report["cursor_team_id"] = os.environ["REVIEW_CURSOR_TEAM_ID"]
    if qualgen:
        report["qualgen"] = qualgen
    if has_text and not passes:
        return dict(report, state="over-budget", estimated_input_tokens=full, prompt_limit=deep_limit, delivered=None), 2
    if args.estimate:
        return dict(report, state="estimate"), 2 if degraded or (has_text and planned_tier != tier) else 0

    completed, coverage, findings, deep_findings, last_error = [], {}, [], [], None
    for row, pass_prompt, locations, blocks in plans:
        depth = "max" if row["name"] == "deep" else first_tier
        answer, error = run_pass(row, pass_prompt, locations, args, change.identity, cap=cap, timeout=TIMEOUTS[depth])
        if answer is None:
            last_error = error
            continue
        completed.append((row["name"], blocks))
        coverage.update({path: lines for path, lines in locations.items() if lines})
        if depth == "max":
            deep_findings = answer["findings"]
        findings = merge_findings(findings, answer["findings"]) if findings else answer["findings"]
    # Report only what reached the model: a block of a pass that failed was not sent.
    sent = merged_fates(blocks for _, blocks in completed)
    report["context"] = [sent.get(name) or (dict(row, state="not sent") if row["state"] in USED else row)
                         for name, row in context_fates.items()]
    report["delivered"] = reached(completed) if completed else None
    if tier != "regular" and findings:
        before = len(findings)
        left = budget - report["estimated_input_tokens"]
        report["verification"] = {"state": "unavailable", "before": before}
        for radius in (15, 8, 3, 0):
            check = verification(findings, files, radius)
            size = tokens(check)
            if size <= left:
                row = {"name": "verification", "estimated_input_tokens": size, "state": "planned"}
                passes.append(row)
                report["estimated_input_tokens"] += size
                verdict, _ = run_pass(row, check, coverage, args, change.identity,
                                      cap=before, timeout=120, originals=findings)
                if verdict is not None:
                    findings = verdict["findings"]
                    report["verification"] = {"state": "filtered", "before": before, "after": len(findings)}
                break
    cap = caps[tier]
    if len(findings) > cap:
        report["omitted_findings"] = len(findings) - cap
        from_deep = [issue for issue in findings if issue in deep_findings]
        from_first = [issue for issue in findings if issue not in deep_findings]
        # A max cap below the mid cap can leave more deep findings than the cap allows.
        from_deep = from_deep[:cap]
        findings = from_first[:cap - len(from_deep)] + from_deep
    report.update(findings=findings, cursor_usage=total_usage(passes),
                  cache_hit=bool(passes) and all(row["state"] == "cached" for row in passes),
                  cursor_invocations=sum(row["model_invocations"] for row in passes))
    if has_text and not completed:
        return dict(report, state="model-unavailable", error=last_error), 2
    degraded = sorted(reviewable - coverage.keys())
    report["degraded"] = degraded
    if report["delivered"] == "max":
        report["prompt_limit"] = deep_limit
    lowered = ((has_text and report["delivered"] != tier) or report["verification"]["state"] == "unavailable"
               or any(row["state"] == "failed" for row in passes))
    incomplete = bool(omitted or any(c["state"] != "complete" for c in [*checks, *static.values()]))
    if degraded or lowered:
        report["state"] = "degraded"
    else:
        report["state"] = "partial" if incomplete else ("reviewed" if has_text else "empty")
    return report, 2 if incomplete or degraded or lowered else 0


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
            if args.sarif:
                args.sarif_resources = stack
            report, status = run_request(args)
            if args.request:
                # At once, before the cleanup or the report can fail.
                settle(args, review_outcome(report))
            if args.sarif:
                # Print the report first, then export while the reviewed checkout is still available.
                print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
                checkout = getattr(args, "sarif_checkout", args.repo)
                sarif.write(old / args.sarif, report, lambda path: blob(checkout, report["head"], path))
                return status
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
