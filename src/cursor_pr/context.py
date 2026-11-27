"""Gather review context from beyond the patch, using local Git objects and the change's own thread.

Every block is plain text for the prompt's untrusted-data section. A gatherer returns "" when
there is nothing to add and None when it could not look.
"""
import re
import subprocess

from .drift import git
from .engine import hunks, rank, tokens
from .source import ReviewError

MID = ("change description", "overlap with the base", "surrounding code", "call sites")
DEEP = ("change description", "overlap with the base", "earlier comments", "whole changed files",
        "call sites in context", "existing tests")
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
    """A file's text at the head, or None when it is over the size limit or not text and is left out."""
    spec = f"{head}:{path}"
    if int(git(repo, "cat-file", "-s", spec)) > MAX_BLOB:
        return None
    try:
        return git(repo, "cat-file", "blob", spec)
    except UnicodeDecodeError:
        return None


def live(files):
    """Changed files that still exist at the head and have a text patch, source files first."""
    rows = [f for f in files if f.patch and not f.absent and f.status not in ("removed", "deleted", "D")]
    return sorted((f for f in rows if rank(f.path)), key=lambda f: (-rank(f.path), f.path))


def numbered(lines, first):
    return "\n".join(f"{number}: {line}" for number, line in enumerate(lines, first))


@guarded
def surrounding(repo, head, files, radius=25):
    """The head lines around each hunk, so a finding can rest on what encloses the change."""
    parts = []
    for f in live(files):
        text = blob(repo, head, f.path)
        if text is None:
            continue
        lines = text.splitlines()
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
            parts.append(f"## {f.path} lines {first}-{last} at the head\n{numbered(lines[first - 1:last], first)}")
    return "\n\n".join(parts)


def symbols(files):
    """Names of functions and types the change defines or edits inside."""
    found = []
    for f in live(files):
        for hunk in hunks(f):
            # The header names the function a hunk sits in; a definition may also be an unchanged line.
            texts = [hunk.section_header or ""] + [line.value for line in hunk if not line.is_removed]
            for text in texts:
                match = DEFINITION.match(text)
                if match and match[1] not in found:
                    found.append(match[1])
    return found[:MAX_SYMBOLS]


@guarded
def call_sites(repo, head, files, radius=0):
    """Where the changed functions and types are used elsewhere at the head."""
    names = symbols(files)
    if not names:
        return ""
    args = ["grep", "-n", "-I", "-w", "-F", "--no-color", "--max-count=4"]
    for name in names:
        args.extend(["-e", name])
    try:
        output = git(repo, *args, head)
    except ReviewError:
        return ""  # git grep exits 1 when nothing matches.
    sites = {name: [] for name in names}
    for row in output.splitlines():
        _, path, number, text = row.split(":", 3)
        definition = DEFINITION.match(text)
        for name in names:
            if (re.search(rf"\b{re.escape(name)}\b", text) and len(sites[name]) < MAX_SITES
                    and not (definition and definition[1] == name)):
                sites[name].append((path, int(number), text.strip()))
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
                cache[path] = None if text is None else text.splitlines()
            if cache[path] is None:
                continue
            first, last = max(1, number - radius), min(len(cache[path]), number + radius)
            parts.append(f"## Use of {name} in {path} lines {first}-{last}\n"
                         + numbered(cache[path][first - 1:last], first))
    return "\n\n".join(parts)


@guarded
def description(change, root, head_repo, fork):
    """What the author says the change does: the forge description, else its commit messages."""
    if change.description.strip():
        return change.description.strip()
    if not fork:
        return ""
    objects = git(root, "rev-parse", "--path-format=absolute", "--git-path", "objects") if head_repo != root else None
    return git(head_repo, "log", "--no-color", "--format=%s%n%b", f"{fork}..{change.head}", alternates=objects)


@guarded
def overlap(root, drifted):
    """What the base changed, since the branch point, in files this change also touches."""
    if drifted.get("state") != "complete":
        raise ReviewError("base drift unavailable")
    if not drifted["both"]:
        return ""
    return git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", "--unified=3",
               drifted["fork"], drifted["base"], "--", *drifted["both"])


@guarded
def whole_files(repo, head, files):
    parts = []
    for f in live(files):
        if rank(f.path) == SOURCE_RANK and (text := blob(repo, head, f.path)) is not None:
            parts.append(f"## {f.path} at the head\n{text}")
    return "\n\n".join(parts)


@guarded
def existing_tests(repo, head, files):
    """Tests that already cover the touched modules and are not part of the change."""
    changed = {f.path for f in files}
    stems = {f.path.rsplit("/", 1)[-1].split(".", 1)[0].lower() for f in live(files) if rank(f.path) == SOURCE_RANK}
    stems = {stem for stem in stems if len(stem) >= 3}
    parts = []
    for path in git(repo, "ls-tree", "-r", "--name-only", head).splitlines():
        name = path.rsplit("/", 1)[-1].lower()
        if (path not in changed and rank(path) == 1 and any(stem in name for stem in stems) and len(parts) < 6
                and (text := blob(repo, head, path)) is not None):
            parts.append(f"## {path} at the head\n{text}")
    return "\n\n".join(parts)


@guarded
def earlier_comments(notes):
    """The change's conversation so far, newest last, each comment bounded."""
    rows = [note["body"].strip() for note in notes if isinstance(note.get("body"), str)]
    rows = [body for body in rows if body and not body.startswith("/review")]
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


def clip(text, allowance):
    """Cut text at a line boundary to fit an allowance in tokens."""
    if tokens(text) <= allowance:
        return text, False
    lines = text.splitlines()
    keep = len(lines)
    while keep and tokens("\n".join(lines[:keep])) > allowance - 8:
        keep = int(keep * 0.8)
    return ("\n".join(lines[:keep]) + "\n[truncated to fit the token budget]" if keep else ""), True


def assemble(names, raw, allowance):
    """Fit the named blocks into an allowance, in order. Returns the text and each block's fate."""
    parts, fates, left = [], [], allowance
    for position, name in enumerate(names):
        text = raw.get(name)
        if text is None:
            fates.append({"name": name, "state": "unavailable"})
            continue
        if not text:
            fates.append({"name": name, "state": "empty"})
            continue
        share = max(0, left) // sum(1 for later in names[position:] if raw.get(later))
        kept, cut = clip(f"# {name.capitalize()}\n{text}", share) if share > 16 else ("", True)
        if kept:
            parts.append(kept)
            left -= tokens(kept) + 2
        fates.append({"name": name, "state": "dropped" if not kept else ("truncated" if cut else "included")})
    return "\n\n".join(parts), fates
