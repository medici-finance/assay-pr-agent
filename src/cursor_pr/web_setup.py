"""One-shot local browser transport for the standard installer."""
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

from . import dialog, setup


class Session:
    def __init__(self):
        self.cv = threading.Condition()
        self.view = {"id": 0, "status": "starting"}
        self.messages = []
        self.answer = None
        self.nonce = None

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
            if self.view["status"] not in ("register", "question") or self.view["id"] != step_id:
                return False
            self.nonce = None
            self.answer = dialog.QuitSetup
            self.view = {"id": step_id, "status": "working"}
            self.cv.notify_all()
            return True

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

    def run(self, state, force_login=False):
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
        with self.cv:
            self.view = {"id": self.view["id"] + 1, **result}
            self.cv.notify_all()


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
            if url.path != "/step":
                return self.reply(403)
            if not self.unlocked():
                return self.reply(401)
            accepted = session.quit(data.get("id")) if data.get("action") == "quit" else session.submit(data)
            self.reply(200 if accepted else 409)

    class Server(ThreadingHTTPServer):
        def service_actions(self):
            with session.cv:
                status = session.view["status"]
                if status == "quit":
                    threading.Thread(target=self.shutdown, daemon=True).start()
                elif status in ("register", "question") and sys.stdin.isatty():
                    # Provider login owns the terminal during a working step.
                    if select.select([sys.stdin], [], [], 0)[0]:
                        if sys.stdin.readline().strip().lower() in ("q", "quit", "/quit"):
                            session.quit(session.view["id"])

    bind = os.getenv("REVIEW_SETUP_BIND", "127.0.0.1")
    with Server((bind, port), Handler) as server:
        worker = threading.Thread(target=session.run, args=(state, force_login), daemon=True)
        worker.start()
        print_key()
        if sys.stdin.isatty():
            print("At a browser question, type q here or click Quit setup to return to your shell.", flush=True)
        server.serve_forever()
    if session.view["status"] == "quit":
        raise dialog.QuitSetup
    return int(session.view["status"] != "complete")
