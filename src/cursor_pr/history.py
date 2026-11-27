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

from .scanners import fence
from .setup import locked
from .source import valid_sha

log = logging.getLogger(__name__)
HEADER = Path("docs/quality/mine.json")
DEFAULT_INTERVAL = 86400
# `qualgen check` prints prose, not structured data: tests/test_history.py reads LINE from each real
# release named here, and any other release leaves the screen unavailable rather than parsed.
VERIFIED_RELEASES = frozenset({"v1.0.94"})
LINE = re.compile(r"(.+?): (NOTICE ([\w-]+) — (.+)|could-not-screen — .+|no advisory signal in this screen)")
UNVERIFIED = "unverified-release"
_verified = None  # True or False once qualgen --version has been read in this process


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


def reported_release(cwd):
    # Run from the tracking root, as for check, and never from inside the repository.
    return subprocess.run(["qualgen", "--version"], cwd=cwd, env={"PATH": os.environ["PATH"]}, check=True, text=True,
                          encoding="utf-8", capture_output=True, timeout=30).stdout.strip()


def verified(cwd):
    global _verified
    if _verified is None:
        _verified = reported_release(cwd) in VERIFIED_RELEASES
        if not _verified:
            # The release qualgen reported is not echoed: it is unverified tool output.
            log.warning("Qualgen on PATH is not a verified release; history screening is unavailable")
    return _verified


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
            if not verified(snapshot):
                return dict(result, reason=UNVERIFIED)
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
        # Paths withheld above never reached Qualgen, so they say nothing about its output.
        sent = set(paths) - set(result["unscreened"])
        unrecognized, seen = 0, set()
        for line in output.splitlines():
            if not line.strip():
                continue
            # A known status for a path this review did not screen is as unexpected as any other text.
            match = LINE.fullmatch(line)
            if not match or match[1] not in sent:
                unrecognized += 1
                continue
            path, status, kind, detail = match.groups()
            seen.add(path)
            if status.startswith("could-not-screen"):
                result["unscreened"].append(path)
            elif kind and (kind != "reference-rot" or same_head):
                result["advisories"].append({"path": path, "kind": kind, "detail": detail})
        missing = sent - seen
        if unrecognized:
            # Advisories that did parse are kept: each matched the verified shape for a screened path,
            # and dropping them would lose real warnings behind a visible format problem. A path with
            # no recognised line stays unscreened, so unreadable output never reads as a clean screen.
            # Only counts and screened paths are kept: check output carries repository text.
            result["unrecognized"] = {"lines": unrecognized, "paths": sorted(missing)}
            log.warning("Qualgen check printed %d line(s) this reviewer could not read", unrecognized)
        result["unscreened"] = sorted(set(result["unscreened"]) | (set(paths) - seen))
        result["state"] = "partial" if result["unscreened"] or unrecognized else "complete"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return result


def summary(result):
    if result["state"] == "unavailable":
        return ["Qualgen history unavailable for this review." +
                (" The installed Qualgen release is not one this reviewer has verified." if result.get("reason") == UNVERIFIED else "")]
    rows = [f"- {a['path']} — {a['kind']}: {a['detail']}" for a in result["advisories"]]
    drift = result.get("unrecognized")
    unreadable = set(drift["paths"]) if drift else ()
    rows.extend(f"- {p} — " + ("Qualgen output could not be read" if p in unreadable else "insufficient history or unable to screen")
                for p in result["unscreened"])
    head = result.get("history_head", "")[:12]
    lines = [f"Qualgen history advisories (baseline {head})" if head else "Qualgen history advisories"]
    note = (f"Qualgen printed {drift['lines']} line{'s' if drift['lines'] > 1 else ''} this reviewer could not read, "
            "so this list may be missing advisories.") if drift else None
    if not rows:
        return lines + [note or "No historical advisories for the selected files."]
    # Leave room in the forge comment for scanner and model findings.
    for row in rows[:20]:
        lines.append(f"- {fence(row[2:], 500)}")
    if len(rows) > 20:
        lines.append(f"{len(rows) - 20} additional Qualgen entries omitted from this comment.")
    if note:
        lines.append(note)
    return lines
