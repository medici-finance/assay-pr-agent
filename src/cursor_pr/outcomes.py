"""What becomes of posted findings: addressed, left unchanged, or raised again at a later review."""
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


def cited(finding, anchors):
    """Keep the cited text and its neighboring lines to distinguish duplicate occurrences."""
    shown = sorted((n, text) for n, value in anchors[finding["path"]].items()
                   if (text := normalize(value)))
    before = sum(n < finding["line"] for n, _ in shown)
    through = sum(n <= finding["end_line"] for n, _ in shown)
    lines = [text for _, text in shown]
    quoted = lines[before:through]
    return {"lines": quoted, "context": context(lines, before, through),
            "duplicates": bool(quoted) and sum(1 for _ in occurrences(lines, quoted)) > 1}


def same_context(old, current):
    return any(before and before == after for before, after in zip(old, current, strict=True))


def still_shown(prior, anchors, start):
    if isinstance(prior, list):
        # Rows written before context was recorded cannot establish where an absent line went.
        lines = {" ".join(text.split()) for text in anchors.values()}
        return True if all(text in lines for text in prior) else None
    lines = [text for _, value in sorted(anchors.items()) if (text := normalize(value))]
    quoted = prior["lines"]
    matches = list(occurrences(lines, quoted))
    if not prior["duplicates"] or not any(prior["context"]):
        if matches:
            return True
    elif any(same_context(prior["context"], context(lines, index, index + len(quoted))) for index in matches):
        return True
    known_context = [line for line in prior["context"] if line]
    if any(line in lines for line in known_context) or (not known_context and start in anchors):
        return False
    # This head's diff may no longer show where the finding was cited.
    return None


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
            else:
                visible = False if reverted else still_shown(prior, anchors[path], start)
                if visible is None:
                    continue
                status = "unaddressed" if visible else "fixed"
            db.execute("UPDATE findings SET status=?, later_head=? WHERE rowid=?", (status, head, rowid))
        for f, key, current in evidence:
            db.execute("INSERT OR IGNORE INTO findings VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (project, number, head, f["path"], f["line"], f["end_line"], key,
                        json.dumps(current), "open", None))
        counts = dict.fromkeys(("fixed", "unaddressed", "repeated", "open"), 0)
        for status, n in db.execute("SELECT status, COUNT(*) FROM findings WHERE project=? AND number=? GROUP BY status",
                                    (project, number)):
            counts[status] = n
        if persist:
            db.commit()
    settled = counts["fixed"] + counts["unaddressed"]
    return dict(counts, accept_rate=round(100 * counts["fixed"] / settled) if settled else None)
