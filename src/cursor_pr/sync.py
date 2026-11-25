"""Refresh only service-managed checkouts with upstream Kubernetes git-sync."""
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import subprocess
import tempfile

from .manager import ConfigurationWatch
from .gitlab_tls import local_url
from .config import load_config
from .credentials import forge_credentials
from .setup import apply_saved_setup, state_dir
from .source import git, reviewer_token_file, ReviewError

log = logging.getLogger(__name__)


def sync_ref(provider, url, root, ref, depth, password, cwd):
    root.mkdir(parents=True, exist_ok=True)
    username = "x-access-token" if provider == "github" else "oauth2"
    # A new token lookup each cycle keeps installation tokens fresh. Only its path goes to git-sync.
    env = {k: os.environ[k] for k in ("HOME", "PATH", "SSL_CERT_FILE", "HTTPS_PROXY", "NO_PROXY") if k in os.environ}
    if provider == "gitlab" and "REVIEW_GITLAB_LOCAL_CA" in os.environ:
        url = local_url(url)
        env.update(GIT_SSL_CAINFO=os.environ["REVIEW_GITLAB_LOCAL_CA"], NO_PROXY="127.0.0.1")
    cmd = ["git-sync", "--one-time", "--repo", url + ".git", "--root", str(root),
           "--ref", ref, "--link", "current", "--depth", str(depth), "--submodules", "off",
           "--stale-worktree-timeout", "1h", "--sync-timeout", "10m",
           "--username", username, "--password-file", str(password)]
    subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=630, check=True)


def sync_project(project, state):
    if project.managed:
        password = reviewer_token_file(project.provider, project.slug, state)
        sync_ref(project.provider, project.url, project.checkout.parent, "HEAD", 0, password, state)


@contextmanager
def scan_checkout(root, project, number, head):
    """Obtain missing PR/MR source for immutable blob scans without writing the shared volume."""
    if project is None or not project.managed:
        yield root
        return
    try:
        git(root, "cat-file", "-e", head + "^{commit}")
    except ReviewError:
        pass
    else:
        yield root
        return
    namespace = "pull" if project.provider == "github" else "merge-requests"
    with tempfile.TemporaryDirectory(prefix="review-change-") as tmp:
        directory = Path(tmp)
        candidate = directory / "current"
        try:
            password = reviewer_token_file(project.provider, project.slug, root)
            sync_ref(project.provider, project.url, directory, f"refs/{namespace}/{number}/head", 1, password, root)
            if git(candidate, "rev-parse", "HEAD").decode().strip() == head:
                root = candidate
        except (OSError, subprocess.SubprocessError, ReviewError):
            log.warning("Change checkout unavailable for %s #%s", project.key, number)
        yield root


def main():
    state = state_dir().resolve()
    marker = Path("/tmp/sync-ready")
    marker.unlink(missing_ok=True)
    watch = ConfigurationWatch(state)
    apply_saved_setup(state, sync_only=True)
    projects = load_config(state)
    seconds = int(os.getenv("REVIEW_SYNC_SECONDS", "120"))
    if seconds < 1:
        raise ValueError("REVIEW_SYNC_SECONDS must be positive")
    with forge_credentials(projects, state):
        while not watch.changed():
            for project in projects.values():
                try:
                    sync_project(project, state)
                except (OSError, subprocess.SubprocessError, ReviewError) as err:
                    log.warning("Repository sync failed for %s (%s); retrying", project.key, type(err).__name__)
            if all(p.checkout.is_dir() for p in projects.values()):
                marker.touch(mode=0o600)
            watch.wait(seconds)
