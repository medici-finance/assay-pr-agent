"""Write the prompt `cursor-review` would send for a local change, without calling any model.

Usage: uv run --frozen --python 3.12 python ci/capture-prompt.py OUT_PREFIX --base REV
           [--max-input-tokens N] [--path FILE ...] [other local cursor-review options]

Writes OUT_PREFIX.prompt.txt (the rubric and the numbered patch) and OUT_PREFIX.anchors.json
(the head lines a finding may cite). Only committed changes between REV and HEAD are included.
See AGENTS.md for how the prompt is used.
"""
import contextlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

from cursor_pr import cli, engine

DEFAULT_BUDGET = "200000"
LOCAL_ONLY = {"change": "--change", "ci": "--ci", "post": "--post", "estimate": "--estimate"}


def fail(message):
    print(f"capture-prompt: {message}", file=sys.stderr)
    return 1


def main(argv):
    if len(argv) < 2 or argv[1].startswith("-"):
        return fail("usage: capture-prompt.py OUT_PREFIX --base REV [cursor-review options]")
    prefix, options = Path(argv[1]), argv[2:]
    args = cli.parser().parse_args(options)
    for name, flag in LOCAL_ONLY.items():
        if getattr(args, name):
            return fail(f"{flag} is not supported: capture a local change with --base")
    if args.tier != "regular":
        return fail("only the regular tier is captured: its prompt is the one the deployed reviewer sends first")
    if args.max_input_tokens is None:
        options += ["--max-input-tokens", DEFAULT_BUDGET]
    for module, name in ((cli, "review"), (cli.providers, "cursor_account"), (engine, "invoke")):
        if not callable(getattr(module, name, None)):
            return fail(f"{module.__name__}.{name} no longer exists; update this script to match cli.py")

    captured = []

    def capture(prompt, model, anchors, cache, source, **_):
        captured.append((prompt, anchors))
        return {"findings": [], "cursor_usage": None}, False

    def refuse(*_, **__):
        raise RuntimeError("capture-prompt must not start a model CLI")

    out = io.StringIO()
    with patch.object(cli, "review", side_effect=capture), \
         patch.object(cli.providers, "cursor_account", contextlib.nullcontext), \
         patch.object(engine, "invoke", side_effect=refuse), \
         patch.object(sys, "argv", ["cursor-review", *options]), \
         contextlib.redirect_stdout(out):
        code = cli.main()
    try:
        report = json.loads(out.getvalue())
    except ValueError:
        return fail(f"cursor-review stopped before producing a report (exit {code}); see the message above")
    if not captured:
        return fail(f"the review never reached the model step (state: {report.get('state')}, "
                    f"estimated input tokens: {report.get('estimated_input_tokens')}, "
                    f"limit: {report.get('prompt_limit')}); nothing was written")
    if len(captured) > 1:
        return fail(f"the review sent {len(captured)} prompts; this script expects one")
    prompt, anchors = captured[0]
    prompt_file = Path(f"{prefix}.prompt.txt")
    anchors_file = Path(f"{prefix}.anchors.json")
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt)
    anchors_file.write_text(json.dumps({path: sorted(lines) for path, lines in anchors.items()}, indent=1))
    print(f"prompt: {prompt_file}")
    print(f"anchors: {anchors_file}")
    print(f"estimated input tokens: {report['estimated_input_tokens']} (limit {report['prompt_limit']})")
    left_out = sorted(set(report.get("omitted") or []) | set(report.get("degraded") or []))
    print("files left out: " + (", ".join(left_out) if left_out else "none"))
    for check in [*report.get("checks", []), *report.get("static_checks", [])]:
        if check.get("state") != "complete":
            print(f"check not complete: {check.get('tool') or check.get('name')} {check.get('state')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
