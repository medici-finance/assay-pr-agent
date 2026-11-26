"""Compare a change with the tip of its base, using local Git objects and no model tokens."""
import os
import re
import subprocess
import threading

from .engine import hunks
from .scanners import fence
from .source import ReviewError, child_env, run

DOCS = (".md", ".rst", ".txt", ".adoc")
SPAN = re.compile(r"`+([^`\n]+)`+")
WORD = re.compile(r"[A-Za-z_][\w-]{3,}")
MAX_NAMES = 60   # names looked up per review
MAX_ROWS = 10    # rows of each kind in the posted summary


def git(repo, *args, alternates=None):
    env = dict(os.environ)
    if alternates:
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = alternates
    return run(["git", "--literal-pathspecs", "-C", str(repo), *args], env=env, timeout=20).decode().strip()


def commit(repo, ref):
    return git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")


def names(files):
    """Code-quoted names on added documentation lines, each with the first line that uses it."""
    found = {}
    for f in files:
        if not f.path.lower().endswith(DOCS) or f.absent or not f.patch:
            continue
        for hunk in hunks(f):
            for line in hunk:
                if not line.is_added:
                    continue
                for span in SPAN.findall(line.value):
                    for word in WORD.findall(span):
                        found.setdefault(word, (f.path, line.target_line_no))
    return dict(sorted(found.items(), key=lambda item: (item[1], item[0]))[:MAX_NAMES])


def present(repo, tree, words, timeout=20):
    """The subset of words that occur as whole words anywhere in a tree."""
    if not words:
        return set()
    args = ["git", "--literal-pathspecs", "-C", str(repo), "grep", "-I", "-F", "-w", "-o", "-h", "--no-color"]
    for word in sorted(words):
        args.extend(["-e", word])
    found = set()
    with subprocess.Popen([*args, tree], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=child_env()) as proc:
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            for raw in proc.stdout:
                found.add(raw.decode("utf8", "replace").rstrip("\n"))
                if found >= words:
                    proc.kill()  # Every name is accounted for; the rest of the tree is not needed.
                    break
        finally:
            timer.cancel()
        code = proc.wait()
    if not found >= words and code not in (0, 1):
        raise ReviewError("git grep did not finish")
    return found & words


def check(root, head, base, files, *, head_repo=None):
    """Report how far the base has moved since the change branched from it.

    root holds the base history. head_repo, when given, is a separate and possibly shallow
    checkout that holds the head; it borrows root's objects only to find the merge base.
    """
    result = {"state": "could-not-check", "behind": None, "both": [], "references": []}
    try:
        tip = commit(root, base)
        if head_repo is None:
            fork = git(root, "merge-base", tip, head)
        else:
            objects = git(root, "rev-parse", "--path-format=absolute", "--git-path", "objects")
            fork = git(head_repo, "merge-base", tip, head, alternates=objects)
        # The merge base is an ancestor of the tip, so everything below reads root alone.
        behind = int(git(root, "rev-list", "--count", f"{fork}..{tip}"))
        result.update(base=tip, fork=fork, behind=behind)
        if behind:
            moved = set(git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only",
                            fork, tip, "--").splitlines())
            result["both"] = sorted(moved & {f.path for f in files})
            wanted = names(files)
            gone = set(wanted) - present(root, tip, set(wanted))
            # Removed on the base means it was there when the change branched. A name that
            # was never in the tree is a new or external one and says nothing about drift.
            removed = present(root, fork, gone)
            result["references"] = sorted(({"path": wanted[n][0], "line": wanted[n][1], "name": n} for n in removed),
                                          key=lambda row: (row["path"], row["line"], row["name"]))
        result["state"] = "complete"
    except (ReviewError, OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def forge(root, checkout, change, managed):
    """Drift for a forge change: the base is a local ref named like the forge's base branch."""
    ref = change.base_ref
    candidates = []
    if re.fullmatch(r"[\w./-]+", ref) and ".." not in ref:
        candidates = [f"refs/remotes/origin/{ref}", f"refs/heads/{ref}"]
    if managed:
        candidates.append("HEAD")  # A managed checkout is synced to the project's default branch.
    for candidate in candidates:
        try:
            base = commit(root, candidate)
        except (ReviewError, OSError, subprocess.SubprocessError):
            continue
        return check(root, change.head, base, change.files, head_repo=None if checkout == root else checkout)
    return {"state": "could-not-check", "behind": None, "both": [], "references": []}


def summary(result):
    if result["state"] != "complete":
        return ["Base drift could not be checked."]
    base = result["base"][:12]
    if not result["behind"]:
        return [f"Base drift (base {base}): none, the head contains the base tip."]
    count = result["behind"]
    lines = [f"Base drift (base {base}): the head is {count} commit{'s' if count != 1 else ''} behind."]
    both = result["both"]
    if both:
        more = f" and {len(both) - MAX_ROWS} more" if len(both) > MAX_ROWS else ""
        lines.append("- Changed on both sides since the branch point: "
                     + ", ".join(fence(path, 200) for path in both[:MAX_ROWS]) + more)
    for row in result["references"][:MAX_ROWS]:
        lines.append(f"- {fence(row['path'], 200)}:{row['line']} — {fence(row['name'], 80)} "
                     "is quoted here but no longer appears on the base.")
    if len(result["references"]) > MAX_ROWS:
        lines.append(f"{len(result['references']) - MAX_ROWS} additional stale references omitted from this comment.")
    return lines
