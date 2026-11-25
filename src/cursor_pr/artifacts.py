"""Read size-limited reports from CI producers for one reviewed change."""
import io
import json
import os
import re
import subprocess
from urllib.parse import quote, urlsplit
import zipfile
import zlib

import httpx

from .checks import import_go_report
from .source import ReviewError, reviewer_token

LIMIT = 8 * 1024 * 1024


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ReviewError("Invalid CI object ID")
    return value


def download(client, url, headers):
    # Object storage uses a signed URL. Never forward a forge token to the redirect.
    for _ in range(4):
        with client.stream("GET", url, headers=headers) as response:
            if response.is_redirect:
                url = response.url.join(response.headers["Location"])
                if url.scheme != "https" or url.userinfo:
                    raise ReviewError("Invalid artifact redirect")
                headers = {}
                continue
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                if len(data) + len(chunk) > LIMIT:
                    raise ReviewError("Artifact exceeds size limit")
                data.extend(chunk)
            return data
    raise ReviewError("Too many artifact redirects")


def github(client, repository, workflow, number, head, headers, *, scanner=None):
    base = f"https://api.github.com/repos/{repository}/actions"
    response = client.get(f"{base}/workflows/{quote(workflow, safe='')}/runs",
                          params={"head_sha": head, "per_page": 1}, headers=headers)
    response.raise_for_status()
    runs = response.json()["workflow_runs"]
    if not runs or runs[0]["head_sha"] != head or runs[0]["status"] != "completed":
        raise ReviewError("The analysis run is missing or still running")
    run = runs[0]
    if run.get("pull_requests") and not any(pr["number"] == int(number) for pr in run["pull_requests"]):
        raise ReviewError("The analysis run belongs to another pull request")
    run_id = positive_id(run["id"])
    attempt = positive_id(run["run_attempt"])
    name = f"go-analysis-lint-{head}-{run_id}-{attempt}"
    if scanner:
        if run.get("conclusion") not in ("success", "failure"):
            raise ReviewError("Scanner workflow did not finish normally")
        name = scanner["artifact"].replace("{attempt}", str(attempt))
    response = client.get(f"{base}/runs/{run_id}/artifacts", params={"name": name}, headers=headers)
    response.raise_for_status()
    matches = [a for a in response.json()["artifacts"] if a["name"] == name and not a["expired"]]
    if len(matches) != 1:
        raise ReviewError("The current run has no unique unexpired report")
    if scanner and attempt > 1 and "{attempt}" not in scanner["artifact"]:
        response = client.get(f"{base}/runs/{run_id}/attempts/{attempt}", headers=headers)
        response.raise_for_status()
        start = response.json()["run_started_at"]
        if not start or matches[0]["created_at"] < start:
            raise ReviewError("Artifact predates this workflow attempt")
    artifact = positive_id(matches[0]["id"])
    data = download(client, f"{base}/artifacts/{artifact}/zip", headers)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if scanner:
            # Actions pull_request runs advertise the source head even when checkout uses the merge ref.
            if run.get("event") != "push" or "review-head.txt" in archive.namelist():
                if report_member(archive, "review-head.txt").decode("ascii").strip() != head:
                    raise ReviewError("Report did not scan the reviewed checkout")
            return report_member(archive, scanner["report"]), run["conclusion"] == "success"
        if len(archive.infolist()) != 1:
            raise ReviewError("Unexpected Go analysis archive")
        return report_member(archive, "report.json")


def report_member(archive, name):
    entries = [item for item in archive.infolist() if item.filename == name]
    if (len(entries) != 1 or entries[0].file_size > LIMIT or entries[0].flag_bits & 1
            or entries[0].compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
        raise ReviewError("Missing or invalid report in CI archive")
    with archive.open(entries[0]) as member:
        return member.read(LIMIT + 1)


def gitlab(client, base, repository, number, job, head, headers, *, scanner=None):
    base += "/projects/" + quote(repository, safe="")
    response = client.get(f"{base}/merge_requests/{number}", headers=headers)
    response.raise_for_status()
    mr = response.json()
    pipeline = mr.get("head_pipeline")
    if (mr.get("sha") != head or not pipeline
            or positive_id(pipeline["project_id"]) != positive_id(mr["target_project_id"])):
        raise ReviewError("No target-project pipeline for this MR head")
    if scanner and pipeline.get("sha") != head:
        raise ReviewError("Pipeline is not for this source commit")
    pipeline_id = positive_id(pipeline["id"])
    # Pipelines can have hundreds of jobs. Never select an older retried job.
    page = 1
    while True:
        response = client.get(f"{base}/pipelines/{pipeline_id}/jobs",
                              params={"page": page, "per_page": 100}, headers=headers)
        response.raise_for_status()
        for row in response.json():
            if row["name"] == job:
                if row["status"] not in ("success", "failed"):
                    raise ReviewError("The analysis job has not finished")
                job_id = positive_id(row["id"])
                if scanner:
                    if row.get("commit", {}).get("id") != head:
                        raise ReviewError("Job is not for this source commit")
                    path = quote(scanner["report"], safe="/")
                    return download(client, f"{base}/jobs/{job_id}/artifacts/{path}", headers), row["status"] == "success"
                return download(client, f"{base}/jobs/{job_id}/artifacts/report.json", headers)
        if not response.headers.get("X-Next-Page") or page >= 20:
            raise ReviewError("The configured Go analysis job was not found")
        page += 1


def fetch_report(url, producer, head, checkout, *, scanner=None):
    target = urlsplit(url)
    match = re.fullmatch(
        r"/([\w.-]+/[\w.-]+)/(pull|-/merge_requests)/([1-9][0-9]*)", target.path)
    if not match:
        raise ReviewError("Invalid change URL")
    slug, kind, number = match.groups()
    provider = "github" if kind == "pull" else "gitlab"
    base = "https://" + target.netloc + "/api/v4"
    if (target.scheme != "https" or target.username is not None or target.query or target.fragment
            or (provider == "github" and target.netloc != "github.com")
            or (provider == "gitlab" and base != os.getenv("GITLAB_API_BASE", "").rstrip("/"))):
        raise ReviewError("The analysis host does not match the configured forge")
    token = reviewer_token(provider, slug, checkout)
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        if provider == "github":
            return github(client, slug, producer, number, head, {"Authorization": "Bearer " + token}, scanner=scanner)
        return gitlab(client, base, slug, number, producer, head, {"PRIVATE-TOKEN": token}, scanner=scanner)


def forge_go_report(url, producer, head, checkout):
    try:
        raw = fetch_report(url, producer, head, checkout)
        slug = urlsplit(url).path.strip("/").split("/")[:2]
        return import_go_report(json.loads(raw), head, repository="/".join(slug))
    except ReviewError as error:
        reason = str(error)
    except httpx.HTTPStatusError as error:
        reason = f"CI artifact API returned HTTP {error.response.status_code}"
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, KeyError, TypeError, AttributeError,
            RecursionError, EOFError, zipfile.BadZipFile, zlib.error, subprocess.TimeoutExpired):
        # HTTP exceptions may contain a signed URL. Keep details out of the public summary.
        reason = "Could not read a valid Go analysis report"
    return {"tool": "go-analysis", "state": "could-not-check", "findings": [], "reason": reason}
