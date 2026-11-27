"""What becomes of posted findings: fixed, left unchanged, gone from the change, or raised again at a later review.

`fixed` needs evidence that the cited code changed: the later diff shows the cited place, next to a
neighboring line recorded when the finding was posted, holding other code. Code that left the diff
without such evidence (the change to that file or hunk was reverted, or a rebase moved it into the
base) is `gone`, and does not count as fixed.
"""
import hashlib
import json
from contextlib import closing
import sqlite3

NAME = "outcomes.sqlite3"
# A finding raised again is tracked by its newer row, so only open and unaddressed rows are resolved again.
OPEN = ("open", "unaddressed")


def digest(finding):
    return hashlib.sha256((finding["title"] + "\n" + finding["message"]).encode()).hexdigest()[:16]


def normalize(line):
    return line.lstrip().rstrip("\r\n")


def occurrences(lines, cited):
    return (index for index in range(len(lines) - len(cited) + 1)
            if lines[index:index + len(cited)] == cited)


def context(lines, start, end):
    return (lines[start - 1] if start else "", lines[end] if end < len(lines) else "")


def contiguous(numbers, low, high):
    return all(n in numbers for n in range(low, high + 1))


def cited(finding, anchors):
    """Keep the cited text and its neighboring lines to distinguish duplicate occurrences.

    `adjacent` says whether each neighbor sits in the same hunk as the cited lines, with nothing
    unshown between them, so that a later diff can show the cited place itself.
    """
    numbers = anchors[finding["path"]]
    shown = sorted((n, text) for n, value in numbers.items() if (text := normalize(value)))
    before = sum(n < finding["line"] for n, _ in shown)
    through = sum(n <= finding["end_line"] for n, _ in shown)
    lines = [text for _, text in shown]
    quoted = lines[before:through]
    adjacent = [bool(quoted) and before > 0 and contiguous(numbers, shown[before - 1][0], shown[before][0]),
                bool(quoted) and through < len(shown) and contiguous(numbers, shown[through - 1][0], shown[through][0])]
    return {"lines": quoted, "context": context(lines, before, through),
            "duplicates": bool(quoted) and sum(1 for _ in occurrences(lines, quoted)) > 1, "adjacent": adjacent}


def replaced_in_diff(prior, anchors):
    """Whether this diff shows the cited place holding other code: evidence that the code changed.

    A recorded neighbor that was adjacent to the cited lines must appear exactly once, and the next
    shown line on the cited side, in the same hunk, must not be the cited line.
    """
    adjacent = prior.get("adjacent") or [False, False]
    shown = sorted((n, text) for n, value in anchors.items() if (text := normalize(value)))
    texts = [text for _, text in shown]
    for side, neighbor in enumerate(prior["context"]):
        if not adjacent[side] or texts.count(neighbor) != 1:
            continue
        index = texts.index(neighbor)
        other = index + 1 if side == 0 else index - 1
        if not 0 <= other < len(shown):
            continue
        low, high = sorted((shown[index][0], shown[other][0]))
        expected = prior["lines"][0] if side == 0 else prior["lines"][-1]
        if contiguous(anchors, low, high) and texts[other] != expected:
            return True
    return False


def same_context(old, current):
    return any(before and before == after for before, after in zip(old, current, strict=True))


def still_shown(prior, anchors):
    """Whether this diff still shows the cited lines, at the cited place when they occur more than once."""
    if isinstance(prior, list):
        lines = {" ".join(text.split()) for text in anchors.values()}
        return all(text in lines for text in prior)
    lines = [text for _, value in sorted(anchors.items()) if (text := normalize(value))]
    quoted = prior["lines"]
    matches = list(occurrences(lines, quoted))
    if not prior["duplicates"] or not any(prior["context"]):
        return bool(matches)
    return any(same_context(prior["context"], context(lines, index, index + len(quoted))) for index in matches)


def repeated_at(path, start, old, prior, evidence):
    for match_digest in (True, False):
        for index, (finding, key, current) in enumerate(evidence):
            if finding["path"] != path or (key == old) != match_digest:
                continue
            if isinstance(prior, list):
                if match_digest:
                    return index
                continue
            context_matches = same_context(prior["context"], current["context"])
            if prior["duplicates"] and any(prior["context"]) and not context_matches:
                continue
            if match_digest or (prior["lines"] == current["lines"]
                                and (finding["line"] == start or context_matches)):
                return index
    return None


def record(state, address, head, findings, anchors, changed_paths=None, *, persist=True):
    """Compute the outcome summary, persisting it only when the review has been posted."""
    project, _, number = address.rpartition("/")
    number = int(number)
    database = state / NAME
    # A preview for the first post does not create a database file.
    with closing(sqlite3.connect(database if persist or database.exists() else ":memory:")) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS findings (
            project TEXT NOT NULL, number INTEGER NOT NULL, head TEXT NOT NULL, path TEXT NOT NULL,
            start INTEGER NOT NULL, "end" INTEGER NOT NULL, digest TEXT NOT NULL, cited TEXT NOT NULL,
            status TEXT NOT NULL, later_head TEXT, PRIMARY KEY(project, number, head, path, start, digest))""")
        if db.execute("PRAGMA user_version").fetchone()[0] < 1:
            # Rows resolved before version 1 were `fixed` as soon as the cited line left the diff.
            # That is what `gone` means now: no evidence that the code changed.
            db.execute("UPDATE findings SET status='gone' WHERE status='fixed'")
            db.execute("PRAGMA user_version=1")
        evidence = [(f, digest(f), cited(f, anchors)) for f in findings]
        unmatched = evidence.copy()
        earlier = db.execute("""SELECT rowid, path, start, digest, cited FROM findings
                             WHERE project=? AND number=? AND head<>? AND status IN (?,?) ORDER BY rowid DESC""",
                             (project, number, head, *OPEN)).fetchall()
        for rowid, path, start, old, raw in earlier:
            reverted = changed_paths is not None and path not in changed_paths
            # A present but unshown file might have been deselected or skipped for the budget.
            if not reverted and not anchors.get(path):
                continue
            prior = json.loads(raw)
            if not (prior if isinstance(prior, list) else prior["lines"]):
                continue
            repeated = repeated_at(path, start, old, prior, unmatched)
            if repeated is not None:
                unmatched.pop(repeated)
                status = "repeated"
            elif reverted:
                # The change no longer touches the file: reverted or rebased away, not fixed.
                status = "gone"
            elif still_shown(prior, anchors[path]):
                status = "unaddressed"
            elif not isinstance(prior, list) and replaced_in_diff(prior, anchors[path]):
                status = "fixed"
            else:
                # The cited code left the diff and the diff does not show what replaced it: a revert or a
                # rebase looks the same as a fix that restored the base's code, so none of them counts.
                status = "gone"
            db.execute("UPDATE findings SET status=?, later_head=? WHERE rowid=?", (status, head, rowid))
        for f, key, current in evidence:
            db.execute("INSERT OR IGNORE INTO findings VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (project, number, head, f["path"], f["line"], f["end_line"], key,
                        json.dumps(current), "open", None))
        counts = dict.fromkeys(("fixed", "unaddressed", "gone", "repeated", "open"), 0)
        for status, n in db.execute("SELECT status, COUNT(*) FROM findings WHERE project=? AND number=? GROUP BY status",
                                    (project, number)):
            counts[status] = n
        if persist:
            db.commit()
    settled = counts["fixed"] + counts["unaddressed"]
    return dict(counts, accept_rate=round(100 * counts["fixed"] / settled) if settled else None)
