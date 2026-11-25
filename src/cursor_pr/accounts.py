"""Resolve forge usernames to the numeric account IDs the saved configuration keeps."""
import os
import re

import httpx

from .config import project_base
from .credentials import TOKEN, read_secret, validate_operator

GITHUB = "https://api.github.com"
LOGIN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}")
cache = {}


def requester_id(value):
    """Accept a numeric account ID or a forge username, which setup resolves to an ID."""
    value = value.strip()
    if re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    if LOGIN.fullmatch(value) and not (value.isascii() and value.isdigit()):
        return value
    raise ValueError("Enter positive numeric account IDs or forge usernames, separated by commas")


def saved_token():
    """The GitLab reviewer token when the container already supplies one."""
    if TOKEN in os.environ or TOKEN + "_FILE" in os.environ:
        return read_secret(TOKEN)
    return None


def body(response):
    try:
        return response.json()
    except ValueError:
        return None


def github_id(login):
    key = ("github", login.lower())
    if key not in cache:
        try:
            response = httpx.get(f"{GITHUB}/users/{login}", timeout=30, follow_redirects=False)
        except httpx.RequestError:
            raise ValueError("Could not reach GitHub to look up usernames. Enter numeric IDs instead.") from None
        user = body(response)
        if not response.is_success or not isinstance(user, dict) or user.get("type") != "User" or type(user.get("id")) is not int:
            raise ValueError(f"GitHub has no user account named {login!r}")
        cache[key] = user["id"]
    return cache[key]


def gitlab_id(login, base, token, *, verify=True):
    key = ("gitlab", base, login.lower())
    if key not in cache:
        try:
            response = httpx.get(f"{base}/api/v4/users", params={"username": login},
                                 headers={"PRIVATE-TOKEN": token}, verify=verify, timeout=30, follow_redirects=False)
        except httpx.RequestError:
            raise ValueError("Could not reach GitLab to look up usernames. Enter numeric IDs instead.") from None
        users = body(response)
        matches = [u for u in users if isinstance(u, dict) and str(u.get("username", "")).casefold() == login.casefold()] if isinstance(users, list) else []
        if not response.is_success or len(matches) != 1 or type(matches[0].get("id")) is not int:
            raise ValueError(f"GitLab has no user named {login!r} visible to the reviewer token")
        cache[key] = matches[0]["id"]
    return cache[key]


def shape(rows):
    """Rows with usernames replaced by a placeholder ID, for validation before lookup."""
    return [{**row, "requester_ids": [1 if isinstance(i, str) else i for i in row["requester_ids"]]} for row in rows]


def pending(rows):
    return sorted({value for row in rows for value in row["requester_ids"] if isinstance(value, str)})


def lookup(provider, value, *, base=None, token=None, verify=True, notice=None):
    """Resolve one username to an account ID.

    GitHub lookups are public. GitLab lookups need the reviewer token; without
    one a GitLab username stays pending for a second pass once credentials exist."""
    if isinstance(value, int):
        return value
    forge = "GitHub"
    if provider == "github":
        ident = github_id(value)
    elif token:
        forge, ident = "GitLab", gitlab_id(value, base, token, verify=verify)
    else:
        return value
    if notice:
        notice(f"Resolved {forge} user {value} to account {ident}.")
    return ident


def resolve(rows, *, token=None, verify=True, notice=None):
    """Copy rows with every username that can be resolved replaced by its account ID."""
    return [{**row, "requester_ids": [lookup(row["provider"], value,
                                             base=project_base(row) if row["provider"] == "gitlab" else None,
                                             token=token, verify=verify, notice=notice) for value in row["requester_ids"]]}
            for row in rows]


def resolve_operator(value, forges, *, base=None, token=None, verify=True):
    """Return the operator's login:numeric-id, looking up a bare username when its forge is known.

    A bare GitLab username stays unresolved until the reviewer token exists. With
    both forges configured the ID is ambiguous, so the explicit form is required."""
    value = value.strip()
    if ":" in value:
        return validate_operator(value)
    login = value
    if not LOGIN.fullmatch(login):
        raise ValueError("Enter the operator's forge username or username:numeric-id")
    if forges == {"github"}:
        return f"{login}:{github_id(login)}"
    if forges == {"gitlab"}:
        return login if not token else f"{login}:{gitlab_id(login, base, token, verify=verify)}"
    raise ValueError("With GitHub and GitLab configured, enter the operator as username:numeric-id")
