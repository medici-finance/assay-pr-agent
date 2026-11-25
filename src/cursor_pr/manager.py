"""Attachable operator console and Compose worker configuration lifecycle."""
import os
import subprocess
import sys
import time


def console():
    if not sys.stdin.isatty():
        raise SystemExit("The manager needs a terminal. Start the container with -dit, then attach.")
    while True:
        print("\nAssay PR Agent   [Enter] Configure in browser   [r] Re-authenticate and configure   [t] Terminal setup",
              flush=True)
        try:
            choice = input().strip().lower()
            if choice not in ("", "r", "t"):
                continue
            args = ["cursor-review-container", "setup"]
            if choice != "t":
                args.append("--web")
            if choice == "r":
                args.append("--reauth")
            subprocess.run(args, check=False)
        except KeyboardInterrupt:
            print("\nReturned to manager.", flush=True)
        except EOFError:
            return


class ConfigurationWatch:
    def __init__(self, state):
        self.link = state / "setup"
        self.enabled = os.getenv("REVIEW_WATCH_SETUP") == "1"
        if self.enabled and not ("REVIEW_PROJECTS" in os.environ
                or any((state / name).exists() for name in ("projects.json", "hooks.json"))):
            while not self.link.is_symlink():
                time.sleep(1)
        self.revision = self.current()

    def current(self):
        try:
            return self.link.readlink()
        except FileNotFoundError:
            return None

    def changed(self):
        return self.enabled and self.current() != self.revision

    def wait(self, seconds):
        if not self.enabled:
            time.sleep(seconds)
            return
        until = time.monotonic() + seconds
        while not self.changed():
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1, remaining))
