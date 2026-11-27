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


def cited(finding, anchors):
    """The lines a finding cites, as text that survives a change of indentation."""
    return [line for line in (" ".join(anchors[finding["path"]][n].split())
                              for n in range(finding["line"], finding["end_line"] + 1)) if line]


def record(state, address, head, findings, anchors):
    """Resolve this change's earlier findings against the reviewed head, then record this review's.

    A finding is fixed when a line it cited is gone from the change, unaddressed when every line it cited
    is still there and the model did not raise it again, and repeated when it did. An earlier finding
    whose file is not in the reviewed scope is left alone: this head says nothing about it.
    """
    project, number = address.rsplit("#", 1)
    with closing(sqlite3.connect(state / NAME)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS findings (
            project TEXT NOT NULL, number INTEGER NOT NULL, head TEXT NOT NULL, path TEXT NOT NULL,
            start INTEGER NOT NULL, "end" INTEGER NOT NULL, digest TEXT NOT NULL, cited TEXT NOT NULL,
            status TEXT NOT NULL, later_head TEXT, PRIMARY KEY(project, number, head, path, start, digest))""")
        shown = {path: {" ".join(text.split()) for text in lines.values()} for path, lines in anchors.items()}
        raised = {(f["path"], digest(f)) for f in findings}
        earlier = db.execute("SELECT rowid, path, digest, cited FROM findings WHERE project=? AND number=? AND head<>? AND status IN (?,?)",
                             (project, int(number), head, *OPEN)).fetchall()
        for rowid, path, old, lines in earlier:
            # Not in scope, or left out for the budget or with no text: this head says nothing about it.
            if not anchors.get(path):
                continue
            lines = json.loads(lines)
            if not lines:
                continue
            status = ("repeated" if (path, old) in raised else
                      "unaddressed" if all(line in shown[path] for line in lines) else "fixed")
            db.execute("UPDATE findings SET status=?, later_head=? WHERE rowid=?", (status, head, rowid))
        for f in findings:
            db.execute("INSERT OR IGNORE INTO findings VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (project, int(number), head, f["path"], f["line"], f["end_line"], digest(f),
                        json.dumps(cited(f, anchors)), "open", None))
    with closing(sqlite3.connect(state / NAME)) as db:
        counts = dict.fromkeys(("fixed", "unaddressed", "repeated", "open"), 0)
        for status, n in db.execute("SELECT status, COUNT(*) FROM findings WHERE project=? AND number=? GROUP BY status",
                                    (project, int(number))):
            counts[status] = n
    settled = counts["fixed"] + counts["unaddressed"]
    return dict(counts, accept_rate=round(100 * counts["fixed"] / settled) if settled else None)
