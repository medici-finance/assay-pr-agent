"""The receipt at the end of every summary: what a review cost and what it covered. Numbers only."""
import json

from unidiff.errors import UnidiffParseError

from .source import ReviewError, hunks


USAGE_KEYS = (("input", ("input_tokens", "inputTokens")),
              ("cached", ("cache_read_input_tokens", "cached_input_tokens", "cacheReadTokens")),
              ("cache_write", ("cache_creation_input_tokens", "cacheWriteTokens")),
              ("output", ("output_tokens", "outputTokens")))


def count_files(files, selected, reviewable, *, validated):
    """Count confirmed text across the change, including paths outside the selection."""
    counts = dict(changed=len(files), text=0, reviewed=0, skipped=0, excluded=0, no_text=0,
                  unavailable=0, counted_only=0)
    for file in files:
        counts["excluded"] += file.path not in selected
        if file.patch_unavailable:
            counts["unavailable"] += 1
            continue
        if file.absent or not file.patch:
            counts["no_text"] += 1
            continue
        if file.path not in validated:
            try:
                hunks(file)
            except (ReviewError, UnidiffParseError):
                counts["unavailable"] += 1
                continue
        counts["text"] += 1
        if file.path in selected and file.path not in reviewable:
            counts["counted_only"] += 1
    return counts


def dedupe_body(body, report):
    """Use stable receipt counts, excluding costs that change on a cached retry."""
    # A pending publication saved before receipts used the original body as its key.
    counts = report.get("coverage")
    if counts is None:
        return body
    value = report["receipt"]
    return json.dumps([body, counts, value["findings"], value["checks"]], sort_keys=True)


def usage_totals(usage):
    """Input, cache and output tokens from any provider CLI's usage object, where it reports them."""
    usage, totals = usage or {}, {}
    for name, keys in USAGE_KEYS:
        for key in keys:
            if type(usage.get(key)) is int and usage[key] >= 0:
                totals[name] = usage[key]
                break
    return totals


def build(report):
    coverage = report.get("coverage") or {}
    reviewed, text = coverage.get("reviewed"), coverage.get("text")
    percent = None
    if reviewed is not None and text:
        percent = round(100 * reviewed / text, 1)
        if 0 < reviewed < text:
            percent = min(99.9, max(0.1, percent))
    checks = [*report["checks"], *(report.get("static_checks") or [])]
    prompt_limit = report.get("prompt_limit", report["max_input_tokens"])
    passes = report.get("passes")
    cached_passes = (sum(row["state"] == "cached" for row in passes) if passes is not None
                     else int(bool(report.get("cache_hit"))))
    return {
        "state": report["state"],
        "provider": report.get("provider"),
        "model_calls": report.get("model_invocations"),
        "cache_hit": report.get("cache_hit"),
        "cached_passes": cached_passes,
        "estimated_input_tokens": report["estimated_input_tokens"],
        "max_input_tokens": report["max_input_tokens"],
        "prompt_limit": prompt_limit,
        "first_prompt_limit": report.get("first_prompt_limit", prompt_limit),
        "full_input_tokens": report.get("full_input_tokens"),
        "usage": usage_totals(report.get("model_usage") or {}),
        "coverage": {"reviewed": reviewed, "text": text, "percent": percent},
        "skipped": coverage.get("skipped", 0), "no_text": coverage.get("no_text", 0),
        "excluded": coverage.get("excluded", 0), "unavailable": coverage.get("unavailable", 0),
        "counted_only": coverage.get("counted_only", 0),
        "findings": {"model": len(report["findings"]), "static": sum(len(c["findings"]) for c in checks),
                     "dropped": len(report.get("dropped") or []),
                     "unchecked": sum(f.get("evidence") == "unverified" for f in report["findings"])},
        "checks": {"complete": sum(c["state"] == "complete" for c in checks), "total": len(checks)},
    }


def render(receipt):
    calls, usage = receipt["model_calls"], receipt["usage"]
    head = f"Receipt: {calls if calls is not None else 'an unknown number of'} model call{'s' if calls != 1 else ''}"
    if receipt["cache_hit"]:
        head += " (a cached result)"
    elif count := receipt["cached_passes"]:
        head += f" ({count} cached pass{'es' if count != 1 else ''})"
    if receipt["state"] == "estimate":
        head = "Receipt: estimate only, no model call"
    elif receipt["state"] == "over-budget":
        head = "Receipt: over budget, no model call"
    limit = receipt["prompt_limit"] if receipt["state"] == "over-budget" else receipt["max_input_tokens"]
    head += f". Prompt {receipt['estimated_input_tokens']:,} of {limit:,} estimated input tokens"
    # A budget refusal already reports the full estimate as its prompt size.
    full = receipt["full_input_tokens"]
    if receipt["state"] != "over-budget" and full is not None and full > receipt["first_prompt_limit"]:
        head += f". The first pass was estimated at {full:,} tokens before trimming"
    input_label = "uncached input" if receipt["provider"] == "claude" and ("cached" in usage or "cache_write" in usage) else "input"
    usage_labels = (("input", input_label), ("cached", "cache read"),
                    ("cache_write", "cache write"), ("output", "output"))
    if usage:
        head += "; the CLI reported " + " and ".join(f"{usage[key]:,} {label}" for key, label in usage_labels if key in usage) + " tokens"
    coverage = receipt["coverage"]
    if coverage["reviewed"] is None:
        foot = "Coverage: counts unavailable for this saved review."
    elif coverage["percent"] is None and receipt["unavailable"]:
        foot = "Coverage: no confirmed text files."
    elif coverage["percent"] is None:
        foot = "Coverage: no text files to review."
    else:
        foot = f"Coverage: {coverage['reviewed']} of {coverage['text']} text files reviewed ({coverage['percent']:g}%)."
    if coverage["reviewed"] is not None:
        foot += (f" Skipped for budget: {receipt['skipped']}. Not reviewable: {receipt['no_text']}. "
                 f"Outside the selection: {receipt['excluded']}.")
        if receipt["unavailable"]:
            foot += f" Unavailable patches: {receipt['unavailable']}."
        if receipt["counted_only"]:
            foot += f" Text counted only: {receipt['counted_only']}."
    findings, checks = receipt["findings"], receipt["checks"]
    foot += (f" Findings: {findings['model']} model and {findings['static']} static; {findings['dropped']} dropped; "
             f"{findings['unchecked']} without a quote to check. Checks: {checks['complete']} of {checks['total']} complete.")
    return [head + ".", foot]
