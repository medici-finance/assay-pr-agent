"""Write the prompt `cursor-review` would send for a local change, without calling any model.

Usage: uv run --frozen --python 3.12 python ci/capture-prompt.py OUT_PREFIX --base REV
           [--head REV] [--repo DIR] [--path FILE ...] [--max-input-tokens N] [--cache-dir DIR]
           [--go-report FILE] [--qualgen-store DIR]

Writes OUT_PREFIX.prompt.txt (the rubric and the numbered patch) and OUT_PREFIX.anchors.json
(the head lines a finding may cite). Only committed changes between REV and HEAD are included.
See AGENTS.md for how the prompt is used.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from cursor_pr import cli, engine

DEFAULT_BUDGET = "200000"
# The only cursor-review options a capture takes. Every other one is refused: it would select a
# source other than a local change, publish, or run a mode this script does not stand in for.
ALLOWED = ("--base", "--head", "--repo", "--path", "--max-input-tokens", "--cache-dir", "--go-report", "--qualgen-store")


def fail(message):
    print(f"capture-prompt: {message}", file=sys.stderr)
    return 1


# Variables with which Git is pointed at a repository other than the one it is run in, as it is
# inside a hook. A capture describes --repo, whatever started it.
ELSEWHERE = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
             "GIT_NAMESPACE", "GIT_PREFIX", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def committed(repo, reported, asked):
    """The commit that was captured, and how many paths of the checkout are in no commit.

    The count is None when it does not apply (another commit than the checkout's was captured) or
    could not be found out.
    """
    head = git(repo, "rev-parse", "--verify", f"{reported or asked or 'HEAD'}^{{commit}}")
    if head is None:
        return reported or asked or "HEAD", None
    if head != git(repo, "rev-parse", "--verify", "HEAD^{commit}"):
        return head[:12], None
    status = git(repo, "status", "--porcelain")
    return head[:12], None if status is None else len(status.splitlines())


def main(argv):
    if len(argv) < 2 or argv[1].startswith("-"):
        return fail("usage: capture-prompt.py OUT_PREFIX --base REV [cursor-review options]")
    prefix, options = Path(argv[1]), argv[2:]
    for name in ELSEWHERE:
        os.environ.pop(name, None)
    prompt_file = Path(f"{prefix}.prompt.txt")
    anchors_file = Path(f"{prefix}.anchors.json")
    # First of all, before any option can be refused: a run that fails for whatever reason must not
    # leave an earlier run's prompt to be reviewed as if it were current.
    try:
        prompt_file.unlink(missing_ok=True)
        anchors_file.unlink(missing_ok=True)
    except OSError as err:
        return fail(f"could not remove an earlier capture ({err}); choose another OUT_PREFIX")
    for option in options:
        if option.startswith("-") and option.split("=", 1)[0] not in ALLOWED:
            return fail(f"{option.split('=', 1)[0]} is not supported: a capture takes only " + ", ".join(ALLOWED))
    args = cli.parser().parse_args(options)
    if not args.base or not args.base.strip():
        return fail("--base REV is required and must not be empty: capture a local change against its base "
                    "(an empty value usually means `git merge-base` failed, as in a shallow clone)")
    if args.max_input_tokens is None:
        options += ["--max-input-tokens", DEFAULT_BUDGET]
    providers = getattr(cli, "providers", None)
    for module, label, name in ((cli, "cursor_pr.cli", "review"), (providers, "cursor_pr.cli.providers", "cursor_account"),
                                (engine, "cursor_pr.engine", "invoke")):
        if not callable(getattr(module, name, None)):
            return fail(f"{label}.{name} no longer exists; update this script to match cli.py")

    captured = []

    def capture(prompt, model, anchors, cache, source, **_):
        captured.append((prompt, anchors))
        return {"findings": [], "cursor_usage": None}, False

    def refuse(*_, **__):
        raise RuntimeError("capture-prompt must not start a model CLI")

    out = io.StringIO()
    try:
        with patch.object(cli, "review", side_effect=capture), \
             patch.object(providers, "cursor_account", contextlib.nullcontext), \
             patch.object(engine, "invoke", side_effect=refuse), \
             patch.object(sys, "argv", ["cursor-review", *options]), \
             contextlib.redirect_stdout(out):
            code = cli.main()
    except SystemExit as stop:
        # cursor-review refused its options or gave up: what it printed must not be lost with it.
        sys.stderr.write(out.getvalue())
        return fail(f"cursor-review exited ({stop.code}) before producing a report; see the message above")
    except Exception:
        sys.stderr.write(out.getvalue())
        raise
    try:
        report = json.loads(out.getvalue())
    except ValueError:
        sys.stderr.write(out.getvalue())
        return fail(f"cursor-review stopped before producing a report (exit {code}); see the message above")
    if not captured:
        return fail(f"the review never reached the model step (state: {report.get('state')}, "
                    f"estimated input tokens: {report.get('estimated_input_tokens')}, "
                    f"limit: {report.get('prompt_limit')}"
                    + (f", error: {report['error']}" if report.get("error") else "") + "); nothing was written")
    if len(captured) > 1:
        return fail(f"the review sent {len(captured)} prompts; this script expects one")
    prompt, anchors = captured[0]
    # Each file is written beside its place and moved there whole, the prompt last: the prompt is what
    # gets reviewed, so it appears only complete and only when all else is in place. Whatever stops
    # this, a failed write, a prompt that cannot be encoded or an interrupt, nothing is left behind.
    staged = {target: target.with_name(target.name + ".part") for target in (anchors_file, prompt_file)}
    contents = {anchors_file: json.dumps({path: sorted(lines) for path, lines in anchors.items()}, indent=1),
                prompt_file: prompt}
    try:
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        for target, part in staged.items():
            part.write_text(contents[target], encoding="utf-8")
        for target, part in staged.items():
            part.replace(target)
    except BaseException as err:
        for path in (*staged.values(), *staged):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if not isinstance(err, (OSError, ValueError)):
            raise
        return fail(f"could not write the capture ({err}); nothing was left behind")
    head, dirty = committed(args.repo, report.get("head"), args.head)
    print(f"prompt: {prompt_file}")
    print(f"anchors: {anchors_file}")
    print(f"captured: {args.base}..{head}")
    print(f"provider: {report.get('provider')} (not called)")
    if dirty is None and not args.head:
        print(f"capture-prompt: warning: could not check {args.repo} for work that is not committed; "
              "only committed changes are in this prompt", file=sys.stderr)
    if dirty:
        # Said loudly: a prompt captured before the last fixes were committed reviews the old change.
        print(f"capture-prompt: warning: {dirty} uncommitted or untracked path(s) in {args.repo} are NOT in this prompt; "
              "commit and capture again if they belong to the change", file=sys.stderr)
    print(f"estimated input tokens: {report['estimated_input_tokens']} (limit {report['prompt_limit']})")
    left_out = sorted(set(report.get("omitted") or []) | set(report.get("degraded") or []))
    print("files left out: " + (", ".join(left_out) if left_out else "none"))
    for check in [*report.get("checks", []), *report.get("static_checks", [])]:
        if check.get("state") != "complete":
            print(f"check not complete: {check.get('tool') or check.get('name')} {check.get('state')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
