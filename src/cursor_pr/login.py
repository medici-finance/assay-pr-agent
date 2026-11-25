"""Native CLI authentication for installed reviewers. Provider CLIs own their credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys

from .providers import LABELS


def auth_mode(provider, value=None):
    mode = os.getenv("REVIEW_AUTH_MODE", "api-key") if value is None else value
    if mode == "api-key" or (mode == "login" and provider in ("claude", "codex")):
        return mode
    raise ValueError(f"Unsupported authentication mode for {provider}: {mode}")


def login_env(provider, directory):
    home = Path(directory).resolve() / provider
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in
           ("PATH", "LANG", "LC_ALL", "TERM", "SSL_CERT_FILE", "HTTPS_PROXY", "NO_PROXY") if key in os.environ}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
    if provider == "codex":
        cache = home / ".codex"
        cache.mkdir(mode=0o700, exist_ok=True)
        env["CODEX_HOME"] = str(cache)
    return env


def login_directory(state):
    return Path(os.getenv("REVIEW_LOGIN_DIR", state / "provider-login"))


def ensure_session(provider, directory, *, interactive=False, force=False):
    if provider == "codex":
        status = ["codex", "login", "status"]
        login = ["codex", "login", "--device-auth"]
    elif provider == "claude":
        status = ["claude", "auth", "status", "--json"]
        login = ["claude", "auth", "login", "--claudeai"]
    else:
        raise ValueError("CLI login is supported for Codex and Claude")
    env = login_env(provider, directory)

    def signed_in():
        result = subprocess.run(status, cwd=env["HOME"], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode or provider == "codex":
            return result.returncode == 0
        try:
            info = json.loads(result.stdout)
        except ValueError:
            return False
        return isinstance(info, dict) and info.get("loggedIn") is True and info.get("authMethod") == "claude.ai"

    if not force and signed_in():
        return
    if not interactive or not sys.stdin.isatty():
        raise ValueError(f"{LABELS[provider]} sign-in requires an installer terminal. "
                         "Run docker compose run --rm --no-deps setup login.")
    print("Continue sign-in with the provider CLI in this terminal.", flush=True)
    if subprocess.run(login, cwd=env["HOME"], env=env).returncode or not signed_in():
        raise ValueError(f"{LABELS[provider]} sign-in did not complete. Run the installer again.")
