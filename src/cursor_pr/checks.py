"""Read immutable blobs and normalize local tooling into compact review evidence."""
from collections import Counter
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from unidiff.errors import UnidiffParseError

from .source import ReviewError, base_line, hunks, child_env, git

BASE_SCAN_SECONDS = 30


def go_report(path, head):
    return import_go_report(json.loads(Path(path).read_bytes()), head)


def import_go_report(value, head, *, repository=None):
    if repository is not None and value.get("repository") != repository:
        raise ReviewError("Go analysis report repository does not match")
    if value.get("schema") != "go-analysis/v1" or value.get("source_sha") != head:
        raise ReviewError("Go analysis report schema or source SHA does not match")
    incomplete = {"tool": "go-analysis", "state": "could-not-check", "findings": []}
    coverage = value.get("coverage")
    if (value.get("state") != "complete" or value.get("errors") != []
            or type(value.get("expected_scans")) is not int
            or not isinstance(coverage, list) or len(coverage) != value["expected_scans"]
            or any(scan.get("state") != "complete" for scan in coverage)):
        return incomplete
    dispositions = ("existing", "new", "worsened", "resolved")
    counts = dict.fromkeys(dispositions, 0)
    for item in value["findings"]:
        disposition = item.get("disposition")
        if disposition not in counts:
            return incomplete
        counts[disposition] += 1
    failed = bool(counts["new"] or counts["worsened"])
    if (value.get("totals") != counts or value.get("exit_code") != int(failed)
            or value.get("gate") != ("fail" if failed else "pass")):
        return incomplete
    findings = [{"path": f["path"], "line": f["line"], "rule": f["rule"],
                 "analyzer": f["analyzer"], "disposition": f["disposition"]}
                for f in value["findings"] if f["disposition"] in ("new", "worsened")]
    if any(type(f["line"]) is not int or f["line"] < 1
           or any(not isinstance(f[k], str) or not f[k] for k in ("path", "rule", "analyzer"))
           for f in findings):
        return incomplete
    return {"tool": "go-analysis", "state": "complete", "findings": findings}


def scanner(path):
    if path.endswith((".sh", ".bash")):
        return "shellcheck", ["--norc", "--format=json1", "-"]
    if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path):
        return "actionlint", ["-shellcheck=", "-pyflakes=", "-format", "{{json .}}", "-"]


def blob(root, head, path, limit=1_000_000):
    """The immutable content of one path at the reviewed head, or None when Git cannot supply it."""
    try:
        spec = head + ":" + path
        if int(git(root, "cat-file", "-s", spec)) > limit:
            return None
        return git(root, "cat-file", "blob", spec)
    except (ReviewError, OSError, ValueError, subprocess.TimeoutExpired):
        return None


def scan_blob(tool, args, content, timeout=20):
    # Use the same scanner and arguments on each side, in a clean directory.
    with tempfile.TemporaryDirectory() as empty:
        proc = subprocess.run([tool, *args], input=content, cwd=empty, env=child_env(),
                              capture_output=True, timeout=timeout)
    if proc.returncode not in (0, 1):
        raise ReviewError("scanner failed")
    rows = json.loads(proc.stdout)
    if tool == "shellcheck":
        rows = rows["comments"]
    elif rows is None:
        rows = []
    if not isinstance(rows, list) or (proc.returncode == 1 and not rows):
        raise ReviewError("scanner exited with an unclassified error")
    return rows


def diagnostic(row, line):
    end = row.get("endLine", row["line"])
    return (line, end - row["line"], row.get("column"), row.get("endColumn"), row.get("level"),
            row.get("code"), row.get("kind"), row["message"])


def changed_diagnostics(file, rows, head, base, tool, args, deadline):
    """Keep rows that cannot be matched to an equivalent diagnostic at the old location."""
    try:
        patch = hunks(file)
        old_lines, head_lines = base.split(b"\n"), head.split(b"\n")
        candidates = []
        for row in rows:
            line, end = row["line"], row.get("endLine", row["line"])
            old = base_line(patch, line)
            if (old is not None and 0 < line <= end <= len(head_lines)
                    and (old_end := base_line(patch, end)) == old + end - line
                    and 0 < old <= old_end <= len(old_lines)
                    and old_lines[old - 1:old_end] == head_lines[line - 1:end]):
                candidates.append(diagnostic(row, old))
            else:
                candidates.append(None)
        remaining = deadline - time.monotonic()
        if not any(candidates) or remaining <= 0:
            return rows
        previous = Counter(diagnostic(row, row["line"]) for row in scan_blob(tool, args, base, min(20, remaining)))
    except (ReviewError, UnidiffParseError, ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired):
        return rows
    new = []
    for row, key in zip(rows, candidates, strict=True):
        if previous[key]:
            previous[key] -= 1
        else:
            new.append(row)
    return new


def scan_files(root, head, files, *, base_root=None, base=""):
    checks = []
    deadline = time.monotonic() + BASE_SCAN_SECONDS
    for file in files:
        path = file.path
        if file.status in ("removed", "deleted", "D") or file.absent:
            continue
        command = scanner(path)
        if not command:
            continue
        tool, args = command
        result = {"tool": tool, "path": path, "state": "could-not-check", "findings": []}
        checks.append(result)
        if not shutil.which(tool):
            result["reason"] = "tool unavailable"
            continue
        try:
            content = blob(root, head, path)
            if content is None:
                raise ReviewError("immutable source unavailable")
            rows = scan_blob(tool, args, content)
            if (rows and base and file.patch and file.status in ("modified", "M")
                    and time.monotonic() < deadline):
                old = blob(base_root if base_root is not None else root, base, file.path)
                if old is not None:
                    kept = changed_diagnostics(file, rows, content, old, tool, args, deadline)
                    if len(kept) != len(rows):
                        result["existing_findings"] = len(rows) - len(kept)
                    rows = kept
            if tool == "shellcheck":
                findings = [{"path": path, "rule": f"SC{r['code']}", "line": r["line"],
                             "message": r["message"]} for r in rows]
            else:
                findings = [{"path": path, "rule": r["kind"], "line": r["line"],
                             "message": r["message"]} for r in rows]
            result.update(state="complete", findings=findings)
        except (ReviewError, ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired):
            result["reason"] = "immutable source unavailable or scanner failed"
    return checks
