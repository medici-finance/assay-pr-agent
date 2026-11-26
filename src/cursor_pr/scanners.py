"""Summary-only scanners. Native reports never enter the model prompt or cache key."""
from collections import defaultdict
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile
import zlib

import httpx

from .artifacts import LIMIT, fetch_report
from .source import ReviewError, git

TOOLS = ("hadolint", "zizmor", "osv-scanner")
LOCKFILES = set("""conan.lock package_config.json pubspec.lock cabal.project.freeze mix.lock go.mod gradle.lockfile buildscript-gradle.lockfile
package-lock.json pnpm-lock.yaml yarn.lock bun.lock packages.lock.json packages.config pom.xml composer.lock Pipfile.lock
poetry.lock requirements.txt pdm.lock pylock.toml uv.lock Gemfile.lock gems.locked Cargo.lock
renv.lock stack.yaml.lock""".split())


def tool_for(path):
    name = PurePosixPath(path).name
    if re.fullmatch(r"(Dockerfile|Containerfile)(\..+)?|.+\.(dockerfile|containerfile)", name, re.I):
        return "hadolint"
    if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path) or name in ("action.yaml", "action.yml"):
        return "zizmor"
    if (name in LOCKFILES or name == "deps.json" or name.endswith(".deps.json")
            or path.endswith("/gradle/verification-metadata.xml") or path == "gradle/verification-metadata.xml"):
        return "osv-scanner"


def inputs(files):
    return {(tool, f.path) for f in files if f.status not in ("removed", "deleted", "D")
            and (tool := tool_for(f.path))}


def ci_config(settings, provider):
    if not isinstance(settings, dict) or settings.keys() - set(TOOLS):
        raise ValueError("scanner_ci must map scanner names to CI artifact settings")
    config = {}
    for tool in TOOLS:
        row = {"producer": tool + (".yml" if provider == "github" else ""),
               "artifact": tool, "report": tool + (".sarif" if tool == "zizmor" else ".json"),
               "all_paths": False, "source_root": ""}
        custom = settings.get(tool, {})
        if not isinstance(custom, dict) or custom.keys() - row.keys():
            raise ValueError("Unknown scanner CI setting")
        row.update(custom)
        if any(not isinstance(row[k], str) or not row[k].isprintable() or len(row[k]) > 255
               for k in ("producer", "artifact", "report")) or type(row["all_paths"]) is not bool:
            raise ValueError("Invalid scanner CI setting")
        if provider == "github" and not re.fullmatch(r"[\w.-]+\.ya?ml|[1-9][0-9]*", row["producer"]):
            raise ValueError("Expected a GitHub scanner workflow filename or ID")
        prefix = row["source_root"]
        if not isinstance(prefix, str) or (prefix and (not prefix.startswith("/") or prefix == "/"
                or not safe_path(prefix.strip("/")))):
            raise ValueError("Expected an absolute CI checkout prefix")
        if not safe_path(row["report"]):
            raise ValueError("Expected a repository-relative scanner report path")
        config[tool] = row
    return config


def safe_path(value):
    return (isinstance(value, str) and bool(value) and value.isprintable()
            and all(part not in ("", ".", "..") for part in value.split("/")))


def array(value):
    if not isinstance(value, list):
        raise ValueError("Invalid scanner report array")
    return value


def native_report(tool, value):
    """Map actual source paths to findings. An absent path has no implied coverage."""
    result = defaultdict(list)

    def add(path, rule, message, line=None):
        if not all(isinstance(v, str) and v.strip() for v in (path, rule, message)):
            raise ValueError("Invalid scanner finding")
        if line is not None and (type(line) is not int or line < 1):
            raise ValueError("Invalid scanner finding line")
        result[path].append({"rule": rule, "message": message, "line": line})

    if tool == "hadolint":
        for r in array(value):
            add(r["file"], r["code"], r["message"], r["line"])
    elif tool == "zizmor":
        runs = array(value["runs"])
        if value["version"] != "2.1.0" or not runs:
            raise ValueError("Invalid SARIF")
        for run in runs:
            if (run["tool"]["driver"]["name"].lower() != "zizmor" or
                    any(i.get("executionSuccessful") is not True for i in run.get("invocations", []))):
                raise ValueError("Unsuccessful zizmor report")
            for r in array(run["results"]):
                loc = r["locations"][0]["physicalLocation"]
                add(unquote(loc["artifactLocation"]["uri"]), r["ruleId"], r["message"]["text"],
                    loc.get("region", {}).get("startLine"))
    else:
        for entry in array(value["results"]):
            path = entry["source"]["path"]
            result.setdefault(path, [])  # --all-packages can explicitly cover a clean file.
            for item in array(entry["packages"]):
                package = item["package"]
                for vuln in array(item.get("vulnerabilities", [])):
                    add(path, vuln["id"], f"{package['name']} {package['version']}")
    return result


READ_ERRORS = (ReviewError, OSError, ValueError, KeyError, TypeError, AttributeError, IndexError,
               RecursionError, EOFError, NotImplementedError, httpx.HTTPError, httpx.InvalidURL,
               zipfile.BadZipFile, zlib.error, subprocess.SubprocessError)


def ci_path(path, url, configured_root):
    if path.startswith("file:///"):
        path = urlsplit(path).path
    slug = "/".join(urlsplit(url).path.split("/-/merge_requests/")[0].split("/pull/")[0].split("/")[-2:])
    repo = slug.split("/")[1]
    roots = [configured_root] if configured_root else (
        [f"/home/runner/work/{repo}/{repo}", f"/__w/{repo}/{repo}"] if "/-/merge_requests/" not in url
        else [f"/builds/{slug}"])
    for root in roots:
        prefix = root.rstrip("/") + "/"
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    path = path.removeprefix("./")
    if not safe_path(path):
        raise ValueError("Cannot map report location to the repository")
    return path


def ci_results(url, head, root, wanted, settings):
    results = {}
    if not url or not wanted:
        return results
    provider = "gitlab" if "/-/merge_requests/" in url else "github"
    config = ci_config(settings, provider)
    for tool in sorted({t for t, _ in wanted}):
        row = config[tool]
        try:
            raw, success = fetch_report(url, row["producer"], head, root, scanner=row)
            native = native_report(tool, json.loads(raw))
            by_path = defaultdict(list)
            for path, findings in native.items():
                path = ci_path(path, url, row["source_root"])
                by_path[path].extend(findings)
            for t, path in wanted:
                if t == tool and ((success and row["all_paths"]) or
                                  (path in by_path and (success or by_path[path]))):
                    results[t, path] = completed(tool, path, "ci", by_path[path])
        except READ_ERRORS:
            continue
    return results


def completed(tool, path, origin, findings):
    return {"tool": tool, "path": path, "source": origin, "state": "complete", "findings": findings}


def local(root, head, tool, path, cache, *, timeout=90):
    result = {"tool": tool, "path": path, "source": "local", "state": "could-not-check", "findings": []}
    try:
        if not safe_path(path):
            raise ValueError("Invalid input path")
        # Reject links/submodules; never read a working-tree path or follow a PR-supplied link.
        tree = git(root, "ls-tree", "-z", head, "--", path)
        if not tree.startswith((b"100644 blob ", b"100755 blob ")):
            raise ValueError("Missing regular blob")
        spec = head + ":" + path
        if int(git(root, "cat-file", "-s", spec)) > LIMIT:
            raise ValueError("Input exceeds size limit")
        blob = git(root, "cat-file", "blob", spec)
        with tempfile.TemporaryDirectory(prefix="review-scan-") as tmp:
            base = Path(tmp)
            target = base / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            env = {"PATH": os.environ["PATH"], "HOME": tmp}
            if tool == "hadolint":
                argv = [tool, "--format", "json", "--no-fail", "./" + path]
            elif tool == "zizmor":
                argv = [tool, "--offline", "--no-exit-codes", "--format", "sarif", "./" + path]
            else:
                cache.mkdir(parents=True, exist_ok=True)
                env["OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY"] = str(cache.resolve())
                argv = [tool, "scan", "source", "--offline", "--format", "json", "--lockfile", "./" + path]
                if os.getenv("REVIEW_OSV_DOWNLOAD_DATABASES", "1") == "1":
                    argv.append("--download-offline-databases")
            with tempfile.TemporaryFile() as out:
                proc = subprocess.run(argv, cwd=base, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                      stderr=subprocess.DEVNULL, timeout=timeout)
                if proc.returncode != 0 and not (tool == "osv-scanner" and proc.returncode == 1):
                    raise ValueError("Scanner did not complete")
                if out.tell() > LIMIT:
                    raise ValueError("Report exceeds size limit")
                out.seek(0)
                native = native_report(tool, json.load(out))
            findings = []
            for source, rows in native.items():
                if (base / source).resolve() != target:
                    raise ValueError("Unexpected scanner location")
                findings.extend(rows)
            if proc.returncode and not findings:
                raise ValueError("Unclassified scanner exit")
        return completed(tool, path, "local", findings)
    except READ_ERRORS:
        result["reason"] = "source, scanner or advisory database unavailable" if tool == "osv-scanner" else "source unavailable or scanner failed"
        return result


def fence(value, limit=None):
    """Quote attacker-influenced text as code so Markdown and @mentions stay literal."""
    text = " ".join(str(value).splitlines())
    if limit:
        text = text[:limit]
    if not text:
        return ""
    marks = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    return f"{marks} {text} {marks}"


def summary(checks):
    lines = ["Static checks"]
    findings = []
    for check in checks:
        label = f"{check['tool']} ({check['source']})"
        if check["state"] != "complete":
            findings.append(f"{label} {check['path']} — could not check: {check['reason']}")
        for row in check["findings"]:
            location = check["path"] + (f":{row['line']}" if row["line"] else "")
            findings.append(f"{label} {location} — {row['rule']} {row['message']}")
    for tool in TOOLS:
        group = [c for c in checks if c["tool"] == tool]
        if group:
            ci = sum(c["source"] == "ci" for c in group)
            count = sum(len(c["findings"]) for c in group)
            incomplete = sum(c["state"] != "complete" for c in group)
            lines.append(f"- {tool}: {ci} CI, {len(group)-ci} local, {count} findings, {incomplete} incomplete.")
    for finding in findings[:30]:
        lines.append(f"- {fence(finding, 450)}")
    if len(findings) > 30:
        lines.append(f"{len(findings)-30} additional entries omitted.")
    return lines
