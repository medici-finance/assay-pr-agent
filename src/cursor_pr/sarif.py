"""Export one review report as a SARIF 2.1.0 log. Every value in it is inert JSON data."""
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import struct
from urllib.parse import quote

from .scanners import safe_path

TOOL = "assay-pr-agent"
PROJECT = "https://github.com/medici-finance/assay-pr-agent"
MESSAGE_LIMIT = 1000
# The report keeps no scanner severity, so the level follows the source: model findings and
# vulnerabilities are what a reader should look at first, every other scanner finding is a note.
WARNINGS = {"model", "osv-scanner"}
COMPLETE = {"reviewed", "empty", "estimate"}
BLOCK = 100
BASE = 37
MASK = (1 << 64) - 1
LEAVING = pow(BASE, BLOCK, 1 << 64)


def release():
    try:
        return version("cursor-pr-review")
    except PackageNotFoundError:
        return "unknown"


def findings(report):
    """(tool, rule, path, start, end, text) for every finding, in the report's order."""
    rows = [("model", "model-finding", f["path"], f["line"], f.get("end_line"), f"{f['title']}. {f['message']}")
            for f in report["findings"]]
    for check in report["checks"]:
        # A go-analysis row carries an analyzer and a disposition where the others carry a message.
        rows += [(check["tool"], f["rule"], f["path"], f["line"], None,
                  f"{f['analyzer']} reported this as {f['disposition']}." if "analyzer" in f else f["message"])
                 for f in check["findings"]]
    for check in report.get("static_checks", []):
        rows += [(check["tool"], f["rule"], check["path"], f["line"], None, f["message"])
                 for f in check["findings"]]
    return rows


def describe(tool, name):
    # The rule name and description are limited to 255 and 1,024 characters on both forges.
    if tool == "model":
        return {"id": name, "name": "Model finding", "shortDescription": {"text": "A finding reported by the review model."}}
    return {"id": name, "name": name[:255], "shortDescription": {"text": f"A finding reported by {tool}."}}


def line_hashes(blob, wanted=None):
    """Line number to primaryLocationLineHash, computed as github/codeql-action's src/fingerprints.ts
    does: a rolling hash of the 100 characters counted from the line's start, ignoring spaces and
    tabs, so the text after the line is part of the value; then that value's number of occurrences in
    the file. No line number is hashed, and lines with the same text stay distinct."""
    window, starts = [0] * BLOCK, [-1] * BLOCK
    seen, prints = {}, {}
    value = index = number = 0
    line_start, after_cr = True, False

    def emit():
        nonlocal value
        text = format(value, "x")
        seen[text] = seen.get(text, 0) + 1
        if wanted is None or starts[index] in wanted:
            prints[starts[index]] = f"{text}:{seen[text]}"
        starts[index] = -1

    def push(current):
        nonlocal value, index
        value = (value * BASE + current - LEAVING * window[index]) & MASK
        window[index] = current
        index = (index + 1) % BLOCK

    def feed(current):
        nonlocal number, line_start, after_cr
        # Spaces and tabs are not hashed. A CR is an LF, and so is the LF that follows a CR.
        if current in (32, 9) or (after_cr and current == 10):
            after_cr = False
            return
        after_cr = current == 13
        if after_cr:
            current = 10
        if starts[index] != -1:
            emit()
        if line_start:
            line_start, number = False, number + 1
            starts[index] = number
        line_start = current == 10
        push(current)

    # The reference reads UTF-16 code units and closes the input with U+FFFF. When that marker
    # starts a line of its own, the line is not in the file and is dropped.
    units = blob.decode("utf8", "replace").encode("utf-16-le")
    for (unit,) in struct.iter_unpack("<H", units):
        feed(unit)
    marker_starts_line = line_start
    feed(0xFFFF)
    for _ in range(BLOCK):
        if starts[index] != -1:
            emit()
        push(0)
    if marker_starts_line:
        prints.pop(number, None)
    return prints


def log(report, source=None):
    """source(path) is the content of a path at the reviewed head, or None when Git cannot supply it."""
    rows = [row for row in findings(report) if safe_path(row[2]) and isinstance(row[1], str)
            and row[1].strip() and isinstance(row[5], str) and row[5].strip()]
    requested = {}
    for _, _, path, line, _, _ in rows:
        if type(line) is int and line > 0:
            requested.setdefault(path, set()).add(line)
    results, rules, lines = [], {}, {}
    for tool, name, path, line, end, text in rows:
        # Six scanners share one driver, so the message names the tool that reported the line.
        text = text if tool == "model" else f"{tool}: {text}"
        # A URI reference: a '#' or '%' in a path must not read as a fragment or an escape.
        location = {"artifactLocation": {"uri": quote(path, safe="/")}}
        result = {"ruleId": name, "level": "warning" if tool in WARNINGS else "note",
                  "message": {"text": text[:MESSAGE_LIMIT]}, "locations": [{"physicalLocation": location}]}
        if type(line) is int and line > 0:
            extent = end if type(end) is int and end >= line else None
            location["region"] = {"startLine": line, **({"endLine": extent} if extent is not None else {})}
            # Use source context and its occurrence count so absolute line offsets do not enter the hash.
            if path not in lines:
                blob = source(path) if source else None
                lines[path] = line_hashes(blob, requested[path]) if blob is not None else {}
            # Without the line's text there is no value: the upload-sarif action computes it instead.
            fingerprint = lines[path].get(line)
            if fingerprint:
                # The finding keeps its own identity when other findings appear on that line.
                span = extent - line if extent is not None else None
                key = hashlib.sha256(json.dumps([span, text]).encode()).hexdigest()
                fingerprint = f"{fingerprint}:{key}"
        else:
            # An OSV-Scanner vulnerability has only the path, rule and message to identify it.
            fingerprint = hashlib.sha256(json.dumps([path, name, text]).encode()).hexdigest()
        if fingerprint:
            result["partialFingerprints"] = {"primaryLocationLineHash": fingerprint}
        rules.setdefault(name, describe(tool, name))
        results.append(result)
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0", "runs": [{
        "tool": {"driver": {"name": TOOL, "version": release(), "informationUri": PROJECT,
                            "rules": list(rules.values())}},
        "invocations": [{"executionSuccessful": report["state"] in COMPLETE}], "results": results}]}


def write(path, report, source=None):
    Path(path).write_text(json.dumps(log(report, source), ensure_ascii=True, indent=2) + "\n", encoding="utf8")
