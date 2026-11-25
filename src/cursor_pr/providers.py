"""Provider credentials and safe, non-interactive CLI settings."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

KEYS = {"cursor": "CURSOR_API_KEY", "claude": "ANTHROPIC_API_KEY", "codex": "CODEX_API_KEY"}
LABELS = {"cursor": "Cursor", "claude": "Claude", "codex": "Codex"}


def provider(value=None):
    name = os.environ.get("REVIEW_PROVIDER", "cursor") if value is None else value
    if name not in KEYS:
        raise ValueError("Choose cursor, claude, or codex")
    return name


def team_command(args, key, directory):
    executable = shutil.which("cursor-agent") or shutil.which("agent")
    if not executable:
        raise ValueError("Cursor CLI is missing")
    env = {k: os.environ[k] for k in ("PATH", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE") if k in os.environ}
    env.update(HOME=str(directory), XDG_CONFIG_HOME=str(directory / ".config"), CURSOR_CONFIG_DIR=str(directory))
    if key:
        env["CURSOR_API_KEY"] = key
    try:
        run = subprocess.run([executable, "team", *args, "--format", "json"], cwd=directory, env=env,
                             timeout=30, capture_output=True, check=True, stdin=subprocess.DEVNULL)
        return json.loads(run.stdout)
    except (ValueError, OSError, subprocess.SubprocessError):
        raise ValueError("Cursor could not verify team membership. Check the key and network access.") from None


def select_team(key, requested, directory, pick=None):
    def memberships():
        data = team_command(["list"], key, directory)
        if not isinstance(data, dict) or not isinstance(data.get("teams"), list):
            raise ValueError("Cursor returned an unknown team list")
        teams = {}
        for team in data["teams"]:
            if (not isinstance(team, dict) or type(team.get("id")) is not int
                    or not isinstance(team.get("name"), str) or type(team.get("isSelectable")) is not bool):
                raise ValueError("Cursor returned invalid team details")
            if team["isSelectable"]:
                teams[str(team["id"])] = team
        if data["teams"] and not teams:
            raise ValueError("This Cursor account has no eligible team")
        return teams
    teams = memberships()
    if not requested and len(teams) > 1:
        choices = ", ".join(f"{t['name']} ({i})" for i, t in teams.items())
        if not pick:
            raise ValueError("Multiple Cursor teams. Set REVIEW_CURSOR_TEAM_ID to one of: " + choices)
        requested = pick(teams)
    chosen = requested or next(iter(teams), "")
    if chosen:
        if chosen not in teams:
            raise ValueError("The selected team ID is not eligible for this Cursor account")
        switched = team_command(["switch", chosen], key, directory)
        if (switched.get("status") != "switched" or str(switched.get("team", {}).get("id")) != chosen
                or memberships().get(chosen, {}).get("isActive") is not True):
            raise ValueError("Cursor did not confirm the selected team")
    return chosen


@contextmanager
def cursor_account():
    if provider() == "cursor" and os.getenv("REVIEW_AUTH_MODE") == "login":
        from .login import login_directory, login_env
        from .setup import state_dir
        directory = Path(login_env("cursor", login_directory(state_dir()))["HOME"])
        select_team(None, os.getenv("REVIEW_CURSOR_TEAM_ID", ""), directory)
        yield
        return
    if provider() != "cursor" or not os.getenv("CURSOR_API_KEY"):
        if provider() == "cursor" and os.getenv("REVIEW_CURSOR_TEAM_ID"):
            raise ValueError("Selecting a Cursor team requires CURSOR_API_KEY")
        yield
        return
    with tempfile.TemporaryDirectory(prefix="review-cursor-account-") as temp:
        chosen = select_team(os.environ["CURSOR_API_KEY"], os.getenv("REVIEW_CURSOR_TEAM_ID", ""), Path(temp))
        updates = {"CURSOR_CONFIG_DIR": temp, "REVIEW_CURSOR_TEAM_ID": chosen}
        previous = {k: os.getenv(k) for k in updates}
        os.environ.update(updates)
        try:
            yield
        finally:
            for k, v in previous.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
