"""The receipt at the end of every summary: what a review cost and what it covered. Numbers only."""
USAGE_KEYS = (("input", ("input_tokens", "inputTokens")),
              ("cached", ("cache_read_input_tokens", "cached_input_tokens", "cacheReadTokens")),
              ("cache_write", ("cache_creation_input_tokens",)),
              ("output", ("output_tokens", "outputTokens")))


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
    coverage = report.get("coverage", {})
    reviewed, text = coverage.get("reviewed", 0), coverage.get("text", 0)
    checks = [*report["checks"], *(report.get("static_checks") or [])]
    return {
        "state": report["state"],
        "provider": report.get("provider"),
        "model_calls": report.get("model_invocations"),
        "cache_hit": report.get("cache_hit"),
        "estimated_input_tokens": report["estimated_input_tokens"],
        "max_input_tokens": report["max_input_tokens"],
        "full_input_tokens": report.get("full_input_tokens"),
        "usage": usage_totals(report.get("model_usage") or {}),
        "coverage": {"reviewed": reviewed, "text": text, "percent": round(100 * reviewed / text) if text else None},
        "skipped": coverage.get("skipped", 0), "no_text": coverage.get("no_text", 0), "excluded": coverage.get("excluded", 0),
        "unavailable": coverage.get("unavailable", 0),
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
    if receipt["state"] == "estimate":
        head = "Receipt: estimate only, no model call"
    elif receipt["state"] == "over-budget":
        head = "Receipt: over budget, no model call"
    head += f". Prompt {receipt['estimated_input_tokens']:,} of {receipt['max_input_tokens']:,} estimated input tokens"
    # A budget refusal already reports the full estimate as its prompt size.
    if receipt["state"] != "over-budget" and receipt["full_input_tokens"] > receipt["max_input_tokens"]:
        head += f"; before budget trimming: {receipt['full_input_tokens']:,}"
    input_label = "uncached input" if receipt["provider"] == "claude" and ("cached" in usage or "cache_write" in usage) else "input"
    usage_labels = (("input", input_label), ("cached", "cache read"),
                    ("cache_write", "cache write"), ("output", "output"))
    if usage:
        head += "; the CLI reported " + " and ".join(f"{usage[key]:,} {label}" for key, label in usage_labels if key in usage) + " tokens"
    coverage = receipt["coverage"]
    if coverage["percent"] is None:
        foot = "Coverage: no text files to review."
    else:
        foot = f"Coverage: {coverage['reviewed']} of {coverage['text']} text files reviewed ({coverage['percent']}%)."
    foot += (f" Skipped for budget: {receipt['skipped']}. Not reviewable: {receipt['no_text']}. "
             f"Outside the selection: {receipt['excluded']}.")
    if receipt["unavailable"]:
        foot += f" Unavailable patches: {receipt['unavailable']}."
    findings, checks = receipt["findings"], receipt["checks"]
    foot += (f" Findings: {findings['model']} model and {findings['static']} static; {findings['dropped']} dropped; "
             f"{findings['unchecked']} without a quote to check. Checks: {checks['complete']} of {checks['total']} complete.")
    return [head + ".", foot]
