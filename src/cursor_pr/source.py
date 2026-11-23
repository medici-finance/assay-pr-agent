"""Read a forge snapshot with Assay, or compare two immutable local Git revisions."""
import json
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


class ReviewError(Exception):
    pass


def run(args, *, cwd=None, timeout=45, data=None, env=None):
    result = subprocess.run(args, input=data, capture_output=True, cwd=cwd, env=env, timeout=timeout)
    if result.returncode:
        raise ReviewError(f"{Path(args[0]).name} {args[1]} exited {result.returncode}")
    return result.stdout


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value)


@dataclass(frozen=True)
class File:
    path: str
    patch: str
    status: str
    absent: bool = False
    previous: str = ""


@dataclass(frozen=True)
class Change:
    head: str
    files: list[File]
    identity: str


def desk(kind, address):
    repo, number = address.rsplit("#", 1)
    data = json.loads(run(["deskread", kind, "--change", address]))
    items = data.get("items") or []
    if (data.get("schema") != 1 or data.get("kind") != kind or data.get("partial")
            or len(items) != 1 or items[0].get("repo") != repo or items[0].get("number") != int(number)):
        raise ReviewError("Incomplete or mismatched deskread " + kind + " envelope")
    return items[0]["changeFiles" if kind == "change-files" else "change"]


def forge_change(address):
    if not re.fullmatch(r"[\w.\-]+/[\w.\-]+#[1-9][0-9]*", address):
        raise ReviewError("Use the configured Assay owner/repo#number address on either forge")
    first = desk("change", address)
    head = first.get("headSha")
    if not valid_sha(head):
        raise ReviewError("Forge did not identify the reviewed head")
    result = desk("change-files", address)
    last = desk("change", address)
    # The file-list envelope has no revision. Bracket it with change observations.
    if any(first.get(k) != last.get(k) for k in ("headSha", "baseRef", "changedFiles", "url")):
        raise ReviewError("Change moved during the forge read")
    rows = result.get("files")
    count = result.get("changedFiles")
    if rows is None and count == 0:
        rows = []
    if (result.get("complete") is not True or not isinstance(rows, list)
            or len(rows) != count or count != first.get("changedFiles")):
        raise ReviewError("Forge file inventory incomplete")
    files = []
    for row in rows:
        if type(row.get("patchAbsent")) is not bool:
            raise ReviewError("Missing patch coverage")
        files.append(File(row["filename"], row.get("patch", ""), row.get("status", ""),
                          row["patchAbsent"], row.get("previousFilename", "")))
    return Change(head, files, first.get("url") or address)


def git(root, *args):
    return run(["git", "--literal-pathspecs", "-C", str(root), *args])


def commit(root, ref):
    return git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def local_change(root, base, head):
    from pr_agent.algo.git_patch_processing import to_hunk_only_patch
    head = commit(root, head)
    base = git(root, "merge-base", commit(root, base), head).decode().strip()
    flags = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color"]
    names = git(root, *flags, "--name-status", "-z", base, head, "--").decode().split("\0")
    files = []
    for status, path in zip(names[0:-1:2], names[1::2], strict=True):
        raw = git(root, *flags, "--unified=3", base, head, "--", path)
        try:
            patch = to_hunk_only_patch(raw.decode("utf8"))
        except UnicodeDecodeError:
            patch = ""
        files.append(File(path, patch, status, not bool(patch)))
    return Change(head, files, str(root.resolve()))


def ci_refs():
    import os
    if os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("GITHUB_EVENT_NAME") == "pull_request":
        pr = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())["pull_request"]
        base, head = pr["base"]["sha"], pr["head"]["sha"]
    elif os.getenv("CI_PIPELINE_SOURCE") == "merge_request_event":
        base = os.environ["CI_MERGE_REQUEST_DIFF_BASE_SHA"]
        head = os.getenv("CI_MERGE_REQUEST_SOURCE_BRANCH_SHA")
        if not head and os.getenv("CI_MERGE_REQUEST_EVENT_TYPE", "detached") == "detached":
            head = os.getenv("CI_COMMIT_SHA")
    else:
        raise ReviewError("Expected a GitHub pull_request or GitLab merge_request_event")
    if not valid_sha(base) or not valid_sha(head):
        raise ReviewError("CI did not provide immutable base and source head SHAs")
    return base, head
