"""Gather review context from beyond the patch, using local Git objects and the change's own thread.

Every block is plain text for the prompt's untrusted-data section. A gatherer returns "" when
there is nothing to add and None when it could not look.
"""
from contextlib import closing
from functools import cache
import os
import re
import subprocess
import threading

from .drift import git
from .engine import rank, tokens
from .providers import LABELS
from .source import ReviewError, base_line, hunks, child_env, git as git_bytes, valid_sha

MID = ("change description", "overlap with the base", "surrounding code", "call sites")
DEEP = ("change description", "overlap with the base", "earlier comments", "whole changed files",
        "call sites in context", "existing tests")
HEADED_BLOCKS = {*MID, *DEEP} - {"change description", "overlap with the base"}
DEFINITION = re.compile(r"^\s*(?:export\s+)?(?:(?:public|private|protected|static|async|abstract)\s+)*"
                        r"(?:def|class|func|function|fn|interface|struct)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w{3,})")
MAX_BLOB = 200_000
MAX_RAW = 400_000
MAX_SYMBOLS = 10
MAX_SITES = 6
SOURCE_RANK = 3


def guarded(gatherer):
    def gather(*args, **kwargs):
        try:
            return gatherer(*args, **kwargs)[:MAX_RAW]
        except (ReviewError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return None
    return gather


def blob(repo, head, path):
    """Read a small text blob, skipping non-blob entries such as submodule pointers."""
    entry = git_bytes(repo, "ls-tree", "-l", "-z", head, "--", path)
    if not entry:
        return None
    metadata, _, _ = entry.partition(b"\t")
    _, kind, object_id, size = metadata.split()
    if kind != b"blob" or int(size) > MAX_BLOB:
        return None
    try:
        return git_bytes(repo, "cat-file", "blob", object_id.decode("ascii")).decode()
    except UnicodeDecodeError:
        return None


def file_lines(text):
    """Split on the newline Git uses for line numbering."""
    return text.removesuffix("\n").split("\n") if text else []


def live(files, *, include_removed=False):
    """Changed files with text patches, source files first."""
    rows = [f for f in files if f.patch and not f.absent
            and (include_removed or f.status not in ("removed", "deleted", "D"))]
    return sorted((f for f in rows if rank(f.path)), key=lambda f: (-rank(f.path), f.path))


def numbered(lines, first):
    return "\n".join(f"{number}: {line}" for number, line in enumerate(lines, first))


def verification_comparison(root, checkout, base, head):
    """Read nearby source for cited locations on demand while both checkouts are available."""
    @cache
    @guarded
    def excerpt_blob(repo, revision, path):
        return blob(repo, revision, path)

    def excerpt(file, start, end, radius):
        if not radius:
            return []
        rows = []
        old = base_line(hunks(file), start, nearest=True)
        for label, repo, revision, path, line in (
                ("Base source", root, base, file.previous or file.path, old),
                ("Head source", checkout, head, file.path, start)):
            text = excerpt_blob(repo, revision, path) if revision and line is not None else None
            if text is not None:
                lines = file_lines(text)
                offset = max(0, line - radius - 1)
                window = lines[offset:line + min(end - start, radius) + radius]
                if window:
                    rows.append(f"{label} ({path})\n" + numbered([line[:240] for line in window], offset + 1))
        return rows

    return excerpt


@guarded
def surrounding(repo, head, files, radius=25):
    """The head lines around each hunk, so a finding can rest on what encloses the change."""
    parts = []
    remaining = MAX_RAW
    for f in live(files):
        text = blob(repo, head, f.path)
        if text is None:
            continue
        lines = file_lines(text)
        spans = []
        for hunk in hunks(f):
            touched = [line.target_line_no for line in hunk if line.target_line_no is not None]
            if not touched:
                continue
            first, last = max(1, min(touched) - radius), min(len(lines), max(touched) + radius)
            if spans and first <= spans[-1][1] + 1:
                spans[-1][1] = max(spans[-1][1], last)
            else:
                spans.append([first, last])
        for first, last in spans:
            separator = "\n\n" if parts else ""
            heading = f"{separator}## {f.path} lines {first}-{last} at the head\n"
            parts.append(heading[:remaining])
            remaining -= len(parts[-1])
            for number in range(first, last + 1):
                if not remaining:
                    break
                newline = "\n" if number > first else ""
                parts.append(f"{newline}{number}: {lines[number - 1]}"[:remaining])
                remaining -= len(parts[-1])
            if not remaining:
                return "".join(parts)
    return "".join(parts)


def symbols(files):
    """Names of functions and types the change defines or edits inside."""
    found = []
    for file in live(files, include_removed=True):
        for hunk in hunks(file):
            # Track the enclosing definition on each side so renames find callers of both names.
            header = DEFINITION.match(hunk.section_header or "")
            before = after = header[1] if header else None
            for line in hunk:
                match = DEFINITION.match(line.value)
                if match:
                    if line.source_line_no is not None:
                        before = match[1]
                    if line.target_line_no is not None:
                        after = match[1]
                touched = before if line.is_removed else after if line.is_added else None
                if touched and touched not in found:
                    found.append(touched)
                    if len(found) == MAX_SYMBOLS:
                        return found
    return found


def git_lines(repo, *args, alternates=None):
    """Stream complete Git output lines up to the context byte cap and deadline."""
    env = child_env()
    if alternates:
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = alternates
    command = ["git", "--literal-pathspecs", "-C", os.fspath(repo), *args]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env) as proc:
        expired = threading.Event()

        def stop():
            expired.set()
            proc.kill()

        deadline = threading.Timer(20, stop)
        deadline.start()
        remaining = MAX_RAW
        try:
            while remaining:
                row = proc.stdout.readline(remaining + 1)
                if not row:
                    code = proc.wait()
                    if expired.is_set() or (code != 0 and (args[0], code) != ("grep", 1)):
                        raise ReviewError("Git context read did not finish")
                    return
                if len(row) > remaining:
                    return
                remaining -= len(row)
                yield row.decode("utf-8", "replace")
        finally:
            deadline.cancel()
            if proc.poll() is None:
                proc.kill()
            proc.wait()


@guarded
def call_sites(repo, head, files, radius=0):
    """Where the changed functions and types are used elsewhere at the head."""
    names = symbols(files)
    if not names:
        return ""
    args = ["grep", "-n", "-z", "-I", "-w", "-F", "--no-color", "--no-column"]
    for name in names:
        args.extend(["-e", name])
    sites = {name: [] for name in names}
    patterns = {name: re.compile(rf"\b{re.escape(name)}\b") for name in names}
    with closing(git_lines(repo, *args, head)) as lines:
        for row in lines:
            fields = row.split("\0", 2)
            if len(fields) != 3 or not fields[0].startswith(head + ":"):
                continue
            location, number, text = fields
            path = location[len(head) + 1:]
            definition = DEFINITION.match(text)
            for name in names:
                if (len(sites[name]) < MAX_SITES and patterns[name].search(text)
                        and not (definition and definition[1] == name)):
                    sites[name].append((path, int(number), text.strip()))
            if all(len(group) >= MAX_SITES for group in sites.values()):
                break
    parts, cache = [], {}
    for name, rows in sites.items():
        if not rows:
            continue
        if not radius:
            parts.append(f"## Uses of {name}\n" + "\n".join(f"{path}:{number}: {text}" for path, number, text in rows))
            continue
        for path, number, _ in rows:
            if path not in cache:
                text = blob(repo, head, path)
                cache[path] = None if text is None else file_lines(text)
            if cache[path] is None:
                continue
            first, last = max(1, number - radius), min(len(cache[path]), number + radius)
            parts.append(f"## Use of {name} in {path} lines {first}-{last}\n"
                         + numbered(cache[path][first - 1:last], first))
    return "\n\n".join(parts)


@guarded
def description(change, root, head_repo, fork):
    """What the author says the change does: the forge description, else its commit messages."""
    metadata = "\n\n".join(text for text in (change.title, change.description) if text)
    if metadata:
        return metadata
    if not fork:
        raise ReviewError("no branch point for the commit messages")
    objects = git(root, "rev-parse", "--path-format=absolute", "--git-path", "objects") if head_repo != root else None
    return "".join(git_lines(head_repo, "log", "--no-show-signature", "--no-color", "--format=%s%n%b",
                             f"{fork}..{change.head}", alternates=objects)).strip()


@guarded
def overlap(root, drifted):
    """What the base changed, since the branch point, in files this change also touches."""
    if drifted.get("state") != "complete" or drifted.get("comparison") == "default branch":
        raise ReviewError("base drift unavailable")
    if not drifted["both"]:
        return ""
    return "".join(git_lines(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color",
                             "--unified=3", drifted["fork"], drifted["base"], "--", *drifted["both"])).strip()


@guarded
def whole_files(repo, head, files):
    parts = []
    remaining = MAX_RAW
    for file in live(files):
        if rank(file.path) == SOURCE_RANK and (text := blob(repo, head, file.path)) is not None:
            separator = "\n\n" if parts else ""
            part = f"{separator}## {file.path} at the head\n{text}"[:remaining]
            parts.append(part)
            remaining -= len(part)
            if remaining == 0:
                break
    return "".join(parts)


@guarded
def existing_tests(repo, head, files):
    """Tests that already cover the touched modules and are not part of the change."""
    changed = {f.path for f in files}
    stems = {f.path.rsplit("/", 1)[-1].split(".", 1)[0].lower() for f in live(files) if rank(f.path) == SOURCE_RANK}
    stems = {stem for stem in stems if len(stem) >= 3}
    parts = []
    if not stems:
        return ""
    tree = git_bytes(repo, "ls-tree", "-r", "-z", head)
    for entry in tree.split(b"\0"):
        metadata, _, raw_path = entry.partition(b"\t")
        if b" blob " not in metadata:
            continue
        try:
            path = raw_path.decode()
        except UnicodeDecodeError:
            continue
        name = path.rsplit("/", 1)[-1].lower()
        if (path not in changed and rank(path) == 1 and any(stem in name for stem in stems) and len(parts) < 6
                and (text := blob(repo, head, path)) is not None):
            parts.append(f"## {path} at the head\n{text}")
    return "\n\n".join(parts)


def is_review_summary(body):
    first, _, remainder = body.partition("\n")
    label, _, head = first.partition(" review of ")
    state = remainder.partition("\n")[0].strip()
    return (label in LABELS.values() and valid_sha(head)
            and state in ("reviewed", "partial", "empty", "degraded", "over-budget", "model-unavailable"))


@guarded
def earlier_comments(notes):
    """The change's conversation so far, newest last, each comment bounded."""
    rows = [note["body"].strip() for note in notes if isinstance(note.get("body"), str)]
    rows = [body for body in rows if body and not body.startswith("/review") and not is_review_summary(body)]
    return "\n\n".join(f"## Comment {index}\n{body[:1500]}" for index, body in enumerate(rows[-10:], 1))


def gather(tier, root, checkout, change, files, drifted, notes):
    """Raw context for a tier. Called while the head checkout is still available."""
    if tier == "regular":
        return {}
    fork = drifted.get("fork", "")
    raw = {"change description": description(change, root, checkout, fork),
           "overlap with the base": overlap(root, drifted),
           "surrounding code": surrounding(checkout, change.head, files),
           "call sites": call_sites(checkout, change.head, files)}
    if tier == "max":
        raw.update({"earlier comments": earlier_comments(notes) if notes is not None else None,
                    "whole changed files": whole_files(checkout, change.head, files),
                    "call sites in context": call_sites(checkout, change.head, files, radius=15),
                    "existing tests": existing_tests(checkout, change.head, files)})
    return raw


def clip(text, allowance, *, headed=False):
    """Clip a block to complete lines, keeping content beyond its generated headings."""
    lines = file_lines(text)
    keep = len(lines)
    cut = tokens(text) > allowance
    if cut:
        while keep and tokens("\n".join(lines[:keep])) > allowance - 8:
            keep = int(keep * 0.8)
    if not any(line.strip() and (not headed or not line.startswith("## ")) for line in lines[1:keep]):
        return "", True
    if cut:
        return "\n".join(lines[:keep]) + "\n[truncated to fit the token budget]", True
    return text, False


def assemble(names, raw, allowance):
    """Fit the named blocks into an allowance, in order. Returns the text and each block's fate."""
    parts, fates, left = [], [], allowance
    for position, name in enumerate(names):
        text = raw.get(name)
        if text is None:
            fates.append({"name": name, "state": "unavailable"})
            continue
        if not text.strip():
            fates.append({"name": name, "state": "empty"})
            continue
        share = max(0, left) // sum(1 for later in names[position:] if raw.get(later))
        kept, cut = (clip(f"# {name.capitalize()}\n{text}", share, headed=name in HEADED_BLOCKS)
                     if share > 16 else ("", True))
        if kept:
            parts.append(kept)
            left -= tokens(kept) + 2
        fates.append({"name": name, "state": "dropped" if not kept else ("truncated" if cut else "included")})
    return "\n\n".join(parts), fates
