"""Build record that ci/publish-image.sh bakes into the container image."""
from dataclasses import dataclass
import json
from pathlib import Path

BUILD_INFO = Path("/opt/reviewer/build-info.json")


@dataclass(frozen=True)
class Build:
    sha: str
    tags: tuple[str, ...]
    built_at: str


def load(path=BUILD_INFO):
    """Read the image's build record, or return None outside a built image."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    sha = str(record.get("sha") or "").strip()
    tags = tuple(str(record.get("tags") or "").split())
    built_at = str(record.get("built_at") or "").strip()
    if not (sha or tags or built_at):
        return None
    return Build(sha, tags, built_at)


def banner(build=None):
    """One line naming the commit, published tags and build time of the running image."""
    build = build or load()
    if build is None:
        return "Assay PR Agent build: unknown (no build record in this image)"
    parts = []
    if build.sha:
        parts.append("commit " + build.sha)
    if build.tags:
        parts.append("tags " + ", ".join(build.tags))
    if build.built_at:
        parts.append("built " + build.built_at)
    return "Assay PR Agent build: " + (", ".join(parts) if parts else "unknown")
