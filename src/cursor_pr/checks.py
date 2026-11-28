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
RELATED_LOCATION = re.compile(r"(at line:)([0-9]+)(?=,col:[0-9]+)")


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


def blob(root, head, path, limit=1_000_000, *, deadline=None):
    """The immutable content of one path at the reviewed head, or None when Git cannot supply it."""
    def read(*args):
        if deadline is None:
            return git(root, "cat-file", *args)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReviewError("blob read time budget exhausted")
        return git(root, "cat-file", *args, timeout=remaining)

    try:
        spec = head + ":" + path
        if int(read("-s", spec)) > limit:
            return None
        return read("blob", spec)
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


def diagnostic(row, line, message=None):
    end = row.get("endLine", row["line"])
    return (line, end - row["line"], row.get("column"), row.get("endColumn"), row.get("level"),
            row.get("code"), row.get("kind"), row["message"] if message is None else message)


def baseline_matches(file, rows, head, base, tool, args, deadline):
    """Find equivalent tool diagnostics at unchanged locations on the base."""
    if time.monotonic() >= deadline:
        return set()
    try:
        patch = hunks(file)
        old_lines, head_lines = base.split(b"\n"), head.split(b"\n")

        def related_location(match):
            line = int(match[2])
            old = base_line(patch, line)
            if (old is None or not 0 < line <= len(head_lines) or not 0 < old <= len(old_lines)
                    or old_lines[old - 1] != head_lines[line - 1]):
                return "at changed location"
            return match[1] + str(old)

        candidates = []
        for row in rows:
            if time.monotonic() >= deadline:
                return set()
            line, end = row["line"], row.get("endLine", row["line"])
            old = base_line(patch, line)
            old_end = old if end == line else base_line(patch, end)
            if (old is not None and 0 < line <= end <= len(head_lines)
                    and old_end == old + end - line
                    and 0 < old <= old_end <= len(old_lines)
                    and old_lines[old - 1:old_end] == head_lines[line - 1:end]):
                message = RELATED_LOCATION.sub(related_location, row["message"]) if tool == "actionlint" else None
                candidates.append(diagnostic(row, old, message))
            else:
                candidates.append(None)
        remaining = deadline - time.monotonic()
        if not any(candidates) or remaining <= 0:
            return set()
        previous = Counter()
        for row in scan_blob(tool, args, base, min(20, remaining)):
            if time.monotonic() >= deadline:
                return set()
            previous[diagnostic(row, row["line"])] += 1
    except (ReviewError, UnidiffParseError, ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired):
        return set()
    matched = set()
    for index, key in enumerate(candidates):
        if time.monotonic() >= deadline:
            break
        if previous[key]:
            previous[key] -= 1
            matched.add(index)
    return matched


def scan_files(root, head, files, *, base_root=None, base=""):
    checks = []
    baseline_remaining = BASE_SCAN_SECONDS
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
            matched = set()
            if rows and base and file.patch and file.status in ("modified", "M") and baseline_remaining > 0:
                started = time.monotonic()
                deadline = started + baseline_remaining
                old = blob(base_root if base_root is not None else root, base, file.path, deadline=deadline)
                if old is None and base_root is not None and base_root != root:
                    old = blob(root, base, file.path, deadline=deadline)
                if old is not None:
                    matched = baseline_matches(file, rows, content, old, tool, args, deadline)
                baseline_remaining -= time.monotonic() - started
            if tool == "shellcheck":
                findings = [{"path": path, "rule": f"SC{r['code']}", "line": r["line"],
                             "message": r["message"]} for r in rows]
            else:
                findings = [{"path": path, "rule": r["kind"], "line": r["line"],
                             "message": r["message"]} for r in rows]
            if matched:
                result["existing_findings"] = len(matched)
                for index in matched:
                    findings[index]["baseline_match"] = True
            result.update(state="complete", findings=findings)
        except (ReviewError, ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired):
            result["reason"] = "immutable source unavailable or scanner failed"
    return checks
