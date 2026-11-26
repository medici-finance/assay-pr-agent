"""Export one review report as a SARIF 2.1.0 log. Every value in it is inert JSON data."""
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path

from .scanners import safe_path

TOOL = "assay-pr-agent"
PROJECT = "https://github.com/medici-finance/assay-pr-agent"
MESSAGE_LIMIT = 1000
# The report keeps no scanner severity, so the level follows the source: model findings and
# vulnerabilities are what a reader should look at first, every other scanner finding is a note.
WARNINGS = {"model", "osv-scanner"}
COMPLETE = {"reviewed", "empty", "estimate"}


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


def log(report):
    results, rules = [], {}
    for tool, name, path, line, end, text in findings(report):
        if not (isinstance(text, str) and text.strip() and safe_path(path)
                and isinstance(name, str) and name.strip()):
            continue
        # Six scanners share one driver, so the message names the tool that reported the line.
        text = (text if tool == "model" else f"{tool}: {text}")[:MESSAGE_LIMIT]
        location = {"artifactLocation": {"uri": path}}
        if type(line) is int and line > 0:
            location["region"] = {"startLine": line, **({"endLine": end} if type(end) is int and end >= line else {})}
        rules.setdefault(name, describe(tool, name))
        results.append({"ruleId": name, "level": "warning" if tool in WARNINGS else "note", "message": {"text": text},
                        "locations": [{"physicalLocation": location}],
                        # The line is part of the identity: one rule often repeats a message in a file.
                        "partialFingerprints": {"primaryLocationLineHash": hashlib.sha256(
                            json.dumps([path, name, line, text]).encode()).hexdigest()}})
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0", "runs": [{
        "tool": {"driver": {"name": TOOL, "version": release(), "informationUri": PROJECT,
                            "rules": list(rules.values())}},
        "invocations": [{"executionSuccessful": report["state"] in COMPLETE}], "results": results}]}


def write(path, report):
    Path(path).write_text(json.dumps(log(report), ensure_ascii=True, indent=2) + "\n", encoding="utf8")
