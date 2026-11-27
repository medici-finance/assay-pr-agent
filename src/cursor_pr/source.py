"""Read a forge snapshot with Assay, or compare two immutable local Git revisions."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

from unidiff import PatchSet

from .credentials import SECRET_INPUTS, private_file, validate_token
from .providers import KEYS

class ReviewError(Exception):
    pass


def child_env(env=None):
    """Environment for git, the forge helper and scanners: never the model provider's key."""
    return {k: v for k, v in (os.environ if env is None else env).items()
            if k not in KEYS.values() and k not in SECRET_INPUTS and not k.startswith("REVIEW_FORGE_")}


# Credential shapes that must never reach a log even if a tool were to print one.
SECRET_SHAPES = tuple(map(re.compile, (
    r"\bgh[pousr]_[A-Za-z0-9]{16,}\b", r"\bgithub_pat_[A-Za-z0-9_]{16,}\b", r"\bglpat-[A-Za-z0-9_-]{16,}\b",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")))
OPAQUE = re.compile(r"PRIVATE KEY-----|^[A-Za-z0-9+/=]{40,}$")  # a PEM header or body line


def failure_reason(result, limit=800):
    """Keep a bounded, redacted diagnostic from stderr. Stdout may hold credentials."""
    kept = []
    for line in result.stderr.decode("utf8", "replace").splitlines():
        line = line.strip()
        if not line or OPAQUE.search(line):
            continue
        for shape in SECRET_SHAPES:
            line = shape.sub("<redacted>", line)
        kept.append(line[:400])
        if len(kept) == 4:
            break
    return " | ".join(kept)[:limit]


def run(args, *, cwd=None, timeout=45, data=None, env=None):
    result = subprocess.run(args, input=data, capture_output=True, cwd=cwd, env=child_env(env), timeout=timeout)
    if result.returncode:
        reason = failure_reason(result)
        raise ReviewError(f"{Path(args[0]).name} {args[1]} exited {result.returncode}" + (f": {reason}" if reason else ""))
    return result.stdout


def reviewer_token_file(provider, slug, checkout):
    directory = os.getenv("REVIEW_FORGE_DIR")
    if not directory:
        raise ReviewError("Configure reviewer credentials before using the forge")
    runtime = Path(directory)
    if provider == "gitlab":
        return runtime / "gitlab.token"
    if provider != "github":
        raise ReviewError("Unknown forge for reviewer credential")
    key = hashlib.sha256(slug.encode()).hexdigest()
    path = runtime / (key + ".token")
    expiry = runtime / (key + ".expires")
    with (runtime / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and expiry.exists():
            until = datetime.fromisoformat(expiry.read_text().strip())
            if until > datetime.now(timezone.utc) + timedelta(minutes=5):
                return path
        request = dict(AppID=os.environ["REVIEW_FORGE_APP_ID"],
                       InstallationID=os.environ["REVIEW_FORGE_INSTALL_ID"],
                       Repository=slug.split("/", 1)[1],
                       PrivateKey=(runtime / "github.pem").read_text())
        minted = json.loads(run(["pr-agent-forge", "mint"], data=json.dumps(request).encode()))
        private_file(path, validate_token(minted["token"]))
        private_file(expiry, minted["expires_at"])
    return path


def reviewer_token(provider, slug, checkout):
    return validate_token(reviewer_token_file(provider, slug, checkout).read_text().strip())


def run_forge(verb, project, *, numbers=(), publication=None, timeout=45):
    if project is None:
        raise ReviewError("Use a --change address from the configured review projects")
    if project.provider == "gitlab":
        base = project.url.removesuffix("/" + project.slug) + "/api/v4"
        if os.getenv("GITLAB_API_BASE", "").rstrip("/") != base:
            raise ReviewError("GITLAB_API_BASE must match the project's HTTPS instance /api/v4")
    request = dict(Forge=project.provider, ProjectURL=project.url, Repository=project.slug,
                   Token=reviewer_token(project.provider, project.slug, project.checkout), Numbers=numbers)
    env = os.environ.copy()
    if project.provider == "gitlab" and "REVIEW_GITLAB_LOCAL_CA" in env:
        request["GitLabCAFile"] = env["REVIEW_GITLAB_LOCAL_CA"]
        request["GitLabLoopback"] = env["REVIEW_GITLAB_LOCAL_ORIGIN"]
    if publication is not None:
        request["Publication"] = publication
    return json.loads(run(["pr-agent-forge", verb], data=json.dumps(request).encode(), env=env, timeout=timeout))


def post_comment(project, number, head, body, state):
    if project is None:
        raise ReviewError("Use a --change address from the configured review projects")
    directory = state / "publications"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(json.dumps([project.key, number, head, body]).encode()).hexdigest()
    receipt = directory / (key + ".posted")
    with (directory / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        posted = receipt.is_file()
        run_forge("comment", project, publication=dict(
            Number=number, Head=head, Body=body, MaxVisibility=project.visibility, AlreadyPosted=posted,
            HumanLogins=[os.environ["REVIEW_FORGE_OPERATOR"]] if os.getenv("REVIEW_FORGE_OPERATOR") else [],
            PrivateRepositories=os.getenv("REVIEW_PRIVATE_REPOSITORIES", "").split(","),
            WithheldIdentifiers=[item.strip().lower() for item in
                                 os.getenv("REVIEW_WITHHELD_IDENTIFIERS", "").split(",") if item.strip()]))
        if not posted:
            private_file(receipt, "posted")


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value)


TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 1500
DESCRIPTION_COMMITS = 32
TRUNCATED = " …[truncated]"


def bounded_text(value, limit):
    # The author's own text is optional context: absent or malformed means empty, and long means cut.
    if not isinstance(value, str) or not value.strip():
        return ""
    value = "".join(c for c in value if c.isprintable() or c in "\n\t").strip()
    return value if len(value) <= limit else value[:limit].rstrip() + TRUNCATED


def hunks(f):
    """Parsed hunks of one file's patch."""
    # Parse under a fixed synthetic name; the source envelope owns the actual filename.
    parsed = PatchSet("--- a/file\n+++ b/file\n" + f.patch.rstrip("\n") + "\n")
    if len(parsed) != 1 or not parsed[0]:
        raise ReviewError("Incomplete or unrecognized patch in " + f.path)
    return parsed[0]


def base_line(patch, line, *, nearest=False):
    """Find the corresponding base line, with an optional nearby anchor for insertions."""
    offset = 0
    for hunk in patch:
        start = hunk.target_start + (hunk.target_length == 0)
        end = start + hunk.target_length
        if line < start:
            break
        if line < end:
            previous = hunk.source_start
            for row in hunk:
                if row.target_line_no == line:
                    return (row.source_line_no or max(1, previous)) if nearest else row.source_line_no
                if row.source_line_no is not None:
                    previous = row.source_line_no
            return None
        offset = hunk.source_start + hunk.source_length + (hunk.source_length == 0) - end
    return line + offset


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
    base: str = ""      # tip of the base as a commit, when the caller named one
    base_ref: str = ""  # base branch name reported by the forge
    title: str = ""
    description: str = ""
    description_truncated: bool = False
    patch_base: str = ""  # immutable preimage commit when the comparison supplies it


def forge_change(address, expected_url=None, *, project=None, with_description=False):
    if not re.fullmatch(r"[\w.\-]+/[\w.\-]+#[1-9][0-9]*", address):
        raise ReviewError("Use an owner/repo#number address from the configured projects")
    repo, number = address.rsplit("#", 1)
    if project is None or repo != project.slug:
        raise ReviewError("Use a --change address from the configured review projects")
    numbers = [int(number)]
    first = run_forge("change", project, numbers=numbers)
    if first.get("url") != (expected_url or project.change_url(number)) or first.get("state") not in ("open", "opened"):
        raise ReviewError("Forge resolved a different or closed change")
    head = first.get("headSha")
    if not valid_sha(head):
        raise ReviewError("Forge did not identify the reviewed head")
    result = run_forge("change-files", project, numbers=numbers)
    if result.get("headSha") != head:
        raise ReviewError("Forge files do not match the reviewed head")
    last = None
    if with_description:
        try:
            last = run_forge("change-with-description", project, numbers=numbers)
        except (OSError, ReviewError, ValueError, subprocess.TimeoutExpired):
            # A helper without optional metadata support can still confirm the change.
            pass
    if last is None:
        last = run_forge("change", project, numbers=numbers)
    if any(first.get(k) != last.get(k) for k in ("headSha", "baseRef", "changedFiles", "url", "state")):
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
    base_ref = first.get("baseRef")
    # The description is context, not evidence, so it is read once and never bracketed like the head.
    title = bounded_text(last.get("title"), TITLE_LIMIT)
    description = bounded_text(last.get("body"), DESCRIPTION_LIMIT)
    truncated = last.get("descriptionTruncated") is True or any(text.endswith(TRUNCATED) for text in (title, description))
    # Evidence from the old side is usable only when both reads agree around the patch fetch.
    base = first.get("baseSha") if first.get("baseSha") == last.get("baseSha") else None
    patch_base = first.get("diffBaseSha") if first.get("diffBaseSha") == last.get("diffBaseSha") else None
    return Change(head, files, first.get("url") or address, base=base if valid_sha(base) else "",
                  base_ref=base_ref if isinstance(base_ref, str) else "", title=title, description=description,
                  description_truncated=truncated, patch_base=patch_base if valid_sha(patch_base) else "")


def git(root, *args):
    return run(["git", "--literal-pathspecs", "-C", str(root), *args])


def commit(root, ref):
    return git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def local_change(root, base, head):
    from pr_agent.algo.git_patch_processing import to_hunk_only_patch
    head = commit(root, head)
    tip = commit(root, base)
    base = git(root, "merge-base", tip, head).decode().strip()
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
    return Change(head, files, str(root.resolve()), base=tip, description=local_description(root, base, head),
                  patch_base=base)


def local_description(root, base, head):
    """Collect bounded optional context from the most recent commits in chronological order."""
    byte_limit = 4 * DESCRIPTION_LIMIT
    output = bytearray()
    deadline = time.monotonic() + 45
    args = ["git", "--literal-pathspecs", "-C", str(root), "log", "--no-show-signature", "--encoding=UTF-8",
            f"-{DESCRIPTION_COMMITS + 1}", "--format=%s%n%n%b%x00", base + ".." + head, "--"]
    try:
        with selectors.DefaultSelector() as ready, \
             subprocess.Popen(args, env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            try:
                ready.register(process.stdout, selectors.EVENT_READ)
                while len(output) <= byte_limit:
                    remaining = max(0, deadline - time.monotonic())
                    if not ready.select(remaining):
                        return ""
                    chunk = os.read(process.stdout.fileno(), byte_limit + 1 - len(output))
                    if not chunk:
                        if process.wait(timeout=max(0, deadline - time.monotonic())):
                            return ""
                        break
                    output.extend(chunk)
            finally:
                if process.poll() is None:
                    process.kill()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""
    # Spend the budget newest-first, then display the retained messages in chronological order.
    clipped = len(output) > byte_limit
    messages = output[:byte_limit].decode("utf8", "replace").split("\0")
    if not clipped:
        messages.pop()  # Trailing separator after the last complete message.
    clipped = clipped or len(messages) > DESCRIPTION_COMMITS
    chosen, remaining = [], DESCRIPTION_LIMIT
    for message in messages[:DESCRIPTION_COMMITS]:
        if remaining <= 0:
            clipped = True
            break
        text = bounded_text(message, len(message))
        if not text:
            continue
        if len(text) > remaining:
            clipped = True
            text = text[:remaining].rstrip()
        chosen.append(text)
        remaining -= len(text) + 2
    description = "\n\n".join(reversed(chosen))
    return description + TRUNCATED if clipped else description


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
