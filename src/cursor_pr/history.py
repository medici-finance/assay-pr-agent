"""Maintain Qualgen's per-project tracking roots and report its changed-path notices."""
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from .setup import locked
from .source import valid_sha

log = logging.getLogger(__name__)
HEADER = Path("docs/quality/mine.json")
DEFAULT_INTERVAL = 86400


def interval():
    seconds = int(os.getenv("REVIEW_QUALGEN_INTERVAL_SECONDS", DEFAULT_INTERVAL))
    if seconds < 0:
        raise ValueError("REVIEW_QUALGEN_INTERVAL_SECONDS must be nonnegative")
    return seconds


def store_for(state, project):
    return state / "qualgen" / hashlib.sha256(project.key.encode()).hexdigest()


def git(repo, *args, check=True):
    return subprocess.run(["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check, timeout=30)


def tip(repo):
    sha = git(repo, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    if not valid_sha(sha):
        raise ValueError("Invalid checkout HEAD")
    return sha


def qualgen(mode, repo, tracking, *args, timeout):
    # git-sync uses a relative gitdir in a linked worktree. go-git needs its physical path.
    return subprocess.run(["qualgen", mode, "--repo", str(repo.resolve()), "--out", str(tracking), *args],
                          cwd=tracking, env={"PATH": os.environ["PATH"]}, check=True, text=True,
                          encoding="utf-8", capture_output=True, timeout=timeout).stdout


def mined_tip(snapshot):
    sha = json.loads((snapshot / HEADER).read_bytes())["tip_sha"]
    if not valid_sha(sha):
        raise ValueError("Invalid Qualgen snapshot")
    return sha


def refresh(repo, store):
    # One miner per state volume. A failed or interrupted write only touches the inactive slot.
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    active = store / "current"
    previous = active.readlink() if active.is_symlink() else None
    head = tip(repo)
    try:
        prior_tip = mined_tip(active) if previous else None
    except (OSError, ValueError, KeyError):
        prior_tip = None
    if head == prior_tip:
        return
    slot = "b" if previous == Path("a") else "a"
    staging = store / slot
    if staging.exists():
        shutil.rmtree(staging)
    if prior_tip and git(repo, "merge-base", "--is-ancestor", prior_tip, head, check=False).returncode == 0:
        shutil.copytree(active, staging)
    else:
        staging.mkdir(mode=0o700)
    qualgen("mine", repo, staging, timeout=3600)
    if mined_tip(staging) != head or tip(repo) != head:
        raise ValueError("Checkout changed during Qualgen mining")
    pending = store / "next"
    pending.unlink(missing_ok=True)
    pending.symlink_to(slot)
    # Readers finish with the old slot before it becomes eligible for reclamation.
    with locked(store, fcntl.LOCK_EX):
        os.replace(pending, active)


def mine_periodically(projects, state, seconds):
    while True:
        for project in projects.values():
            try:
                refresh(project.checkout, store_for(state, project))
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as err:
                log.warning("Qualgen refresh failed for %s (%s)", project.key, type(err).__name__)
        time.sleep(seconds)


def screen(repo, store, paths, head):
    result = {"state": "unavailable", "advisories": [], "unscreened": []}
    if not paths:
        return dict(result, state="complete")
    try:
        with locked(store, fcntl.LOCK_SH):
            snapshot = store / "current"
            result["history_head"] = mined_tip(snapshot)
            # check also consults checkout HEAD for live reference-rot notices.
            same_head = tip(repo) == head
            literals = []
            for path in sorted(paths):
                if (not path.isprintable() or Path(path).is_absolute() or ".." in Path(path).parts):
                    result["unscreened"].append(path)
                    continue
                # Qualgen treats wildcard characters as globs. Escape literal metacharacters
                # for Go's filepath.Match, and keep missing glob paths visible as unscreened.
                if any(c in path for c in "*?["):
                    if not (repo / path).exists():
                        result["unscreened"].append(path)
                        continue
                    path = re.sub(r"([\\*?\[])", r"\\\1", path)
                literals.append(path)
            output = qualgen("check", repo, snapshot, "--", *literals, timeout=60) if literals else ""
            same_head = same_head and tip(repo) == head
        seen = set()
        for line in output.splitlines():
            match = re.fullmatch(r"(.+?): (NOTICE ([\w-]+) — (.+)|could-not-screen — .+|no advisory signal in this screen)", line)
            if not match or match[1] not in paths:
                continue
            path, status, kind, detail = match.groups()
            seen.add(path)
            if status.startswith("could-not-screen"):
                result["unscreened"].append(path)
            elif kind and (kind != "reference-rot" or same_head):
                result["advisories"].append({"path": path, "kind": kind, "detail": detail})
        result["unscreened"] = sorted(set(result["unscreened"]) | (set(paths) - seen))
        result["state"] = "partial" if result["unscreened"] else "complete"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return result


def summary(result):
    if result["state"] == "unavailable":
        return ["Qualgen history unavailable for this review."]
    rows = [f"- {a['path']} — {a['kind']}: {a['detail']}" for a in result["advisories"]]
    rows.extend(f"- {p} — insufficient history or unable to screen" for p in result["unscreened"])
    head = result.get("history_head", "")[:12]
    lines = [f"Qualgen history advisories (baseline {head})" if head else "Qualgen history advisories"]
    if not rows:
        return lines + ["No historical advisories for the selected files."]
    # Leave room in the forge comment for scanner and model findings.
    for row in rows[:20]:
        # Quote paths and notice text as code so Markdown and mentions stay literal.
        text = " ".join(row[2:].splitlines())[:500]
        fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
        lines.append(f"- {fence} {text} {fence}")
    if len(rows) > 20:
        lines.append(f"{len(rows) - 20} additional Qualgen entries omitted from this comment.")
    return lines
