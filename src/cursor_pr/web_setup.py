"""One-shot local browser transport for the standard installer and its settings page."""
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import select
import sys
import threading
import re
import time
from urllib.parse import parse_qs, urlsplit

from . import dialog, settings, setup


SAVED = "Saved. The sync and reviewer services reload it between cycles."
UNREADABLE = ("The saved settings could not be read to show them here. "
              "Reload the page to try again, or run the setup wizard.")


class Session:
    def __init__(self):
        self.cv = threading.Condition()
        self.view = {"id": 0, "status": "starting"}
        self.messages = []
        self.answer = None
        self.nonce = None
        self.editing = threading.Lock()  # one settings-page request against one rendered view at a time

    def message(self, text):
        self.messages.append(text)

    def choose(self, label, values):
        answer = self.ask(label, choices=[{"value": v, "label": v} for v in values])
        if answer not in values:
            raise ValueError("Select one of the displayed options")
        return answer

    def register(self, nonce, form):
        with self.cv:
            self.nonce = nonce
            self.view = {"id": self.view["id"] + 1, "status": "register", **form}
            if not self.cv.wait_for(lambda: self.answer is not None, timeout=3600):
                self.nonce = None
                raise ValueError("GitHub registration expired. Start setup again.")
            return self.take_answer()

    def take_answer(self):
        answer, self.answer = self.answer, None
        if answer is dialog.QuitSetup:
            raise dialog.QuitSetup
        return answer

    def quit(self, step_id):
        with self.cv:
            if self.view["status"] not in ("register", "question", "settings") or self.view["id"] != step_id:
                return False
            self.nonce = None
            if self.view["status"] == "settings":
                # No wizard is waiting on an answer here.
                self.view = {"id": step_id, "status": "quit", "label": "Settings closed."}
            else:
                self.answer = dialog.QuitSetup
                self.view = {"id": step_id, "status": "working"}
            self.cv.notify_all()
            return True

    def terminal_quit(self):
        # Use the same lock as the browser to let a save finish before closing.
        with self.editing, self.cv:
            return self.quit(self.view["id"])

    def callback(self, query):
        state, code = query.get("state", []), query.get("code", [])
        with self.cv:
            if (self.nonce is None or len(state) != 1 or not state[0].isascii()
                    or not secrets.compare_digest(state[0], self.nonce) or len(code) != 1
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", code[0])):
                return False
            self.nonce = None
            self.answer = code[0]
            self.view = {"id": self.view["id"], "status": "working"}
            self.cv.notify_all()
            return True

    def ask(self, label, *, secret=False, multiline=False, choices=None, link=None, upload=False):
        with self.cv:
            self.view = {"id": self.view["id"] + 1, "status": "question", "label": label.strip(),
                         "secret": secret, "multiline": multiline, "messages": self.messages, "choices": choices, "link": link, "upload": upload}
            self.messages = []
            self.cv.notify_all()
            self.cv.wait_for(lambda: self.answer is not None)
            return self.take_answer()

    def submit(self, data):
        with self.cv:
            if (self.view["status"] != "question" or data.get("id") != self.view["id"] or self.answer is not None):
                return False
            if not isinstance(data.get("answer"), str):
                return False
            self.answer = data["answer"]
            self.view = {"id": self.view["id"], "status": "working"}
            self.cv.notify_all()
            return True

    def show(self, page, *notices):
        with self.cv:
            if self.view["status"] == "quit":
                return None
            self.view = {"id": self.view["id"] + 1, "status": "settings", "messages": list(notices), **page}
            self.cv.notify_all()
            return self.view.copy()

    def unread(self, *notices):
        """Replace a settings page that can no longer be shown; the next load reads it again."""
        return self.show({"sections": [], "projects": [], "unread": True}, *notices)

    def reread(self, state):
        """Read the settings again for a page that could not be read back after a save."""
        with self.cv:
            if not self.view.get("unread"):
                return
        with self.editing:
            with self.cv:
                if not self.view.get("unread"):
                    return
            try:
                page = settings.view(state)
            except Exception:
                with self.cv:
                    self.view["messages"] = [UNREADABLE]
                return
            self.show(page)

    def launch(self, state, *, force_login=False, resume=False):
        """Start the wizard; one started from the settings page returns to it afterwards."""
        with self.cv:
            self.view = {"id": self.view["id"] + 1, "status": "starting"}
            self.cv.notify_all()
        threading.Thread(target=self.run, args=(state, force_login, resume), daemon=True).start()

    def run(self, state, force_login=False, resume=False):
        token = dialog.browser.set(self)
        try:
            setup.configure(state, force_login=force_login)
            result = {"status": "complete"}
        except dialog.QuitSetup:
            result = {"status": "quit", "label": "Setup closed."}
        except (ValueError, OSError, EOFError) as err:
            result = {"status": "error", "label": str(err)}
        except Exception:
            result = {"status": "error", "label": "Setup could not finish. Restart the installer and try again."}
        finally:
            dialog.browser.reset(token)
        if resume and result["status"] != "quit":
            # The settings page shows what the wizard saved, or why it failed.
            try:
                self.show(settings.view(state), "The setup wizard saved the configuration."
                          if result["status"] == "complete" else result["label"])
                return
            except (ValueError, OSError, KeyError, TypeError):
                pass
        with self.cv:
            self.view = {"id": self.view["id"] + 1, **result}
            self.cv.notify_all()


def change_setting(session, state, data):
    """Apply one settings-page change against the view the browser rendered.

    Returns the HTTP status and the JSON reply: 409 for a stale view, 400 when
    nothing was saved, and 200 with the new view once the save committed. If the
    settings cannot be read back after a committed save, the 200 view says so
    ("unread") rather than reporting the save as failed.
    """
    with session.editing:
        with session.cv:
            current = session.view["status"] == "settings" and session.view["id"] == data.get("id")
            revision = session.view.get("revision")
        if not current or revision is None:
            return 409, None
        try:
            settings.save(state, data, revision=revision)
        except settings.StaleSettings:
            session.unread("The settings changed in another installer. Review the refreshed settings.")
            return 409, None
        except (ValueError, OSError) as err:
            return 400, {"error": str(err)}
        except Exception:
            # Like the wizard, never echo an unexpected failure's details.
            return 400, {"error": "The setting could not be saved. Try again or use the setup wizard."}
        # The save has committed: from here on, the reply must not say otherwise.
        try:
            page = settings.view(state)
        except Exception:
            view = session.unread(SAVED, UNREADABLE)
        else:
            view = session.show(page, SAVED)
    if view is None:
        return 409, None
    return 200, view


def serve(state, *, force_login=False):
    session = Session()
    # Random per wizard session. The installer console is the only place it is
    # printed; the page asks for it and keeps it in a session cookie.
    key = secrets.token_urlsafe(32)
    port = 8765
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    cookie = "pr_agent_setup"
    last_reprint = [float("-inf")]
    reprint_interval = 5.0  # console spam guard only

    def print_key():
        print(f"Setup key: {key}", flush=True)
        print(f"Open http://127.0.0.1:{port}/ and enter the setup key.", flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, data=b"", mime="application/json", headers=()):
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action https://github.com; base-uri 'none'")
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def same_origin(self):
            # State-changing requests must come from the page this server served.
            return self.headers.get("Origin") == "http://" + self.headers["Host"]

        def unlocked(self):
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except CookieError:
                return False
            morsel = jar.get(cookie)
            return morsel is not None and secrets.compare_digest(morsel.value.encode(), key.encode())

        def json_body(self):
            try:
                size = int(self.headers.get("Content-Length", 0))
                if size <= 0:
                    return self.reply(400)
                if size > 1048576:
                    return self.reply(413)
                data = json.loads(self.rfile.read(size))
            except (ValueError, UnicodeError):
                return self.reply(400)
            if not isinstance(data, dict):
                return self.reply(400)
            return data

        def do_GET(self):
            if self.headers.get("Host") not in hosts:
                return self.reply(403)
            url = urlsplit(self.path)
            if url.path == "/github/callback":
                if not session.callback(parse_qs(url.query)):
                    return self.reply(403)
                return self.reply(200, b"GitHub registration received. Return to the installer tab.", "text/plain")
            assets = {"/": ("setup.html", "text/html"), "/setup.js": ("setup.js", "text/javascript"),
                      "/setup.css": ("setup.css", "text/css")}
            if self.path in assets:
                name, mime = assets[self.path]
                return self.reply(200, Path(__file__).with_name(name).read_bytes(), mime + "; charset=utf-8")
            if self.path != "/step":
                return self.reply(403)
            if not self.unlocked():
                return self.reply(401)
            session.reread(state)
            with session.cv:
                view = session.view.copy()
            self.reply(200, json.dumps(view).encode())
            if view["status"] in ("complete", "error"):
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        def do_POST(self):
            if self.headers.get("Host") not in hosts or not self.same_origin():
                return self.reply(403)
            url = urlsplit(self.path)
            if url.path == "/print-key":
                # Unauthenticated by design: the key only goes to the installer console.
                if time.monotonic() - last_reprint[0] < reprint_interval:
                    return self.reply(429)
                last_reprint[0] = time.monotonic()
                print_key()
                return self.reply(204)
            if self.headers.get_content_type() != "application/json":
                return self.reply(403)
            data = self.json_body()
            if data is None:
                return
            if url.path == "/login":
                candidate = data.get("key")
                if not isinstance(candidate, str) or not secrets.compare_digest(candidate.encode(), key.encode()):
                    return self.reply(401)
                return self.reply(200, headers=[("Set-Cookie", f"{cookie}={key}; Path=/; HttpOnly; SameSite=Strict")])
            if url.path not in ("/step", "/setting"):
                return self.reply(403)
            if not self.unlocked():
                return self.reply(401)
            if url.path == "/setting":
                return self.change(data)
            with session.editing:
                if data.get("action") == "wizard":
                    with session.cv:
                        accepted = session.view["status"] == "settings" and session.view["id"] == data.get("id")
                    if accepted:
                        session.launch(state, resume=True)
                else:
                    accepted = session.quit(data.get("id")) if data.get("action") == "quit" else session.submit(data)
            self.reply(200 if accepted else 409)

        def change(self, data):
            code, reply = change_setting(session, state, data)
            self.reply(code, b"" if reply is None else json.dumps(reply).encode())

    class Server(ThreadingHTTPServer):
        def service_actions(self):
            with session.cv:
                status = session.view["status"]
            if status == "quit":
                threading.Thread(target=self.shutdown, daemon=True).start()
            elif status in ("register", "question", "settings") and sys.stdin.isatty():
                # Provider login owns the terminal during a working step.
                if select.select([sys.stdin], [], [], 0)[0]:
                    if sys.stdin.readline().strip().lower() in ("q", "quit", "/quit"):
                        threading.Thread(target=session.terminal_quit, daemon=True).start()

    bind = os.getenv("REVIEW_SETUP_BIND", "127.0.0.1")
    with Server((bind, port), Handler) as server:
        try:
            # A saved setup opens on its settings; the wizard stays one click away.
            page = None if force_login or not (state / "setup").is_symlink() else settings.view(state)
        except (ValueError, OSError, KeyError, TypeError):
            page = None  # An unreadable setup gets the wizard that can replace it.
        if page is None:
            session.launch(state, force_login=force_login)
        else:
            session.show(page)
        print_key()
        if sys.stdin.isatty():
            print("At a browser question or on the settings page, type q here to return to your shell.", flush=True)
        server.serve_forever()
    if session.view["status"] == "quit":
        raise dialog.QuitSetup
    return int(session.view["status"] != "complete")
