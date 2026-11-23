"""Read immutable blobs and normalize local tooling into compact review evidence."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .source import ReviewError, git


def go_report(path, head):
    value = json.loads(Path(path).read_text())
    if value.get("schema") != "go-analysis/v1" or value.get("source_sha") != head:
        raise ReviewError("Go analysis report schema or source SHA does not match")
    incomplete = {"tool": "go-analysis", "state": "could-not-check", "findings": []}
    coverage = value.get("coverage")
    if (value.get("state") != "complete" or value.get("errors") != []
            or not isinstance(coverage, list) or len(coverage) != value.get("expected_scans")
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
    return {"tool": "go-analysis", "state": "complete", "findings": findings}


def scan_files(root, head, files):
    checks = []
    for file in files:
        path = file.path
        if file.status in ("removed", "deleted", "D") or file.absent:
            continue
        if path.endswith((".sh", ".bash")):
            tool, args = "shellcheck", ["--norc", "--format=json1", "-"]
        elif re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path):
            tool, args = "actionlint", ["-shellcheck=", "-pyflakes=", "-format", "{{json .}}", "-"]
        else:
            continue
        result = {"tool": tool, "path": path, "state": "could-not-check", "findings": []}
        checks.append(result)
        if not shutil.which(tool):
            result["reason"] = "tool unavailable"
            continue
        try:
            spec = head + ":" + path
            if int(git(root, "cat-file", "-s", spec)) > 1_000_000:
                raise ReviewError("file exceeds scanner size limit")
            blob = git(root, "cat-file", "blob", spec)
            # stdin in a clean directory avoids project-local config and external source files.
            with tempfile.TemporaryDirectory() as empty:
                proc = subprocess.run([tool, *args], input=blob, cwd=empty, capture_output=True, timeout=20)
            if proc.returncode not in (0, 1):
                raise ReviewError("scanner failed")
            rows = json.loads(proc.stdout)
            if tool == "shellcheck":
                findings = [{"path": path, "rule": f"SC{r['code']}", "line": r["line"],
                             "message": r["message"]} for r in rows["comments"]]
            else:
                findings = [{"path": path, "rule": r["kind"], "line": r["line"],
                             "message": r["message"]} for r in (rows or [])]
            if proc.returncode == 1 and not findings:
                raise ReviewError("scanner exited with an unclassified error")
            result.update(state="complete", findings=findings)
        except (ReviewError, ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired):
            result["reason"] = "immutable source unavailable or scanner failed"
    return checks
