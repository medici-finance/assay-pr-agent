"""Interactive forge bootstrap. Only operational credentials reach the saved setup."""
import base64
import hashlib
import json
import re
import secrets
import subprocess
import time
from urllib.parse import quote

import httpx

from .credentials import APP_ID, INSTALL_ID, validate_pem, validate_token, private_file

CALLBACK = "http://127.0.0.1:8765/github/callback"
API = "https://api.github.com"


def request(method, url, **kwargs):
    try:
        result = httpx.request(method, url, timeout=30, follow_redirects=False, **kwargs)
    except httpx.RequestError:
        raise ValueError("Could not reach the forge. Check the host and connection before retrying.") from None
    if not result.is_success:
        raise ValueError(f"Forge returned HTTP {result.status_code}. Check authorization and repository access.")
    try:
        data = result.json()
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    raise ValueError("The forge returned an invalid response.")


def pending_file(state, scope):
    # The exchange code and created token can only be retrieved once. Resume an
    # interrupted wizard with this credential until the configuration is committed.
    return state / (".provision-" + hashlib.sha256(scope.encode()).hexdigest() + ".json")


def manifest(owner, personal):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*", owner):
        raise ValueError("Expected a GitHub account name")
    state = secrets.token_urlsafe(32)
    prefix = "" if personal else "/organizations/" + owner
    return state, {
        "action": f"https://github.com{prefix}/settings/apps/new?state={state}",
        "manifest": {"url": "https://github.com/medici-finance/assay-pr-agent",
                     "redirect_url": CALLBACK, "public": False,
                     # GitHub rejects the manifest without a webhook URL, even inactive.
                     "hook_attributes": {"url": "https://github.com/medici-finance/assay-pr-agent", "active": False},
                     "default_events": [], "default_permissions": {
                         "contents": "read", "pull_requests": "write", "actions": "read"}},
    }


def github_installation(app, pem_path, projects):
    def encoded(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    now = int(time.time())
    payload = json.dumps({"iss": str(app), "iat": now - 60, "exp": now + 300}).encode()
    message = encoded(b'{"alg":"RS256","typ":"JWT"}') + b"." + encoded(payload)
    signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(pem_path)],
                               input=message, capture_output=True, check=True).stdout
    jwt = (message + b"." + encoded(signature)).decode()
    ids = {request("GET", f"{API}/repos/{p.slug}/installation",
                   headers={"Authorization": "Bearer " + jwt}).get("id") for p in projects}
    if len(ids) != 1 or any(type(i) is not int or i <= 0 for i in ids):
        raise ValueError("Install this App on every selected repository under one GitHub account.")
    return str(ids.pop())


def github(session, state, projects, pem_path):
    owners = {p.slug.split("/")[0].lower() for p in projects}
    if len(owners) != 1:
        raise ValueError("A GitHub App installation covers one owner. Configure one owner per reviewer.")
    owner = owners.pop()
    pending = pending_file(state, "github/" + owner)
    if not pending.exists():
        kind = session.choose(f"Which type of GitHub account owns {owner}?", ["organization", "personal"])
        nonce, form = manifest(owner, kind == "personal")
        code = session.register(nonce, form)
        data = request("POST", f"{API}/app-manifests/{code}/conversions")
        if (type(data.get("id")) is not int or not isinstance(data.get("slug"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]+", data["slug"])):
            raise ValueError("GitHub returned invalid App details")
        app = {"id": data["id"], "slug": data["slug"], "pem": validate_pem(data.get("pem", ""))}
        private_file(pending, json.dumps(app))
    app = json.loads(pending.read_text())
    private_file(pem_path, app["pem"])
    link = "https://github.com/apps/" + app["slug"] + "/installations/new"
    while True:
        session.ask("Install the App on the selected repositories, then continue here.", link=link)
        try:
            installation = github_installation(app["id"], pem_path, projects)
            break
        except ValueError as err:
            session.message(str(err))
    return {APP_ID: str(app["id"]), INSTALL_ID: installation}, pending


def gitlab(session, state, projects, *, verify=True):
    first = projects[0]
    host = first.url.removesuffix("/" + first.slug)
    if len(projects) == 1:
        resource, target = "projects", first.slug
    else:
        groups = {p.slug.split("/")[0] for p in projects}
        if len(groups) != 1:
            raise ValueError("Automatic provisioning requires one GitLab group. Use an existing token for multiple groups.")
        resource, target = "groups", groups.pop()
    url = f"{host}/api/v4/{resource}/{quote(target, safe='')}/access_tokens"
    pending = pending_file(state, url)
    if not pending.exists():
        bootstrap = validate_token(session.ask(
            f"Create a Reporter token for {resource[:-1]} {target} on {host}. Enter a personal access token with api scope (used once).",
            secret=True))
        token = request("POST", url, verify=verify, headers={"PRIVATE-TOKEN": bootstrap},
                        json={"name": "assay-pr-agent", "scopes": ["api", "read_repository"], "access_level": 20})
        private_file(pending, json.dumps(validate_token(token.get("token", ""))))
    return json.loads(pending.read_text()), pending
