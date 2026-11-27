"""Run the actual forge bridge without a home directory or desk settings."""
import http.server
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from cursor_pr import discovery, source
from cursor_pr.config import Project
from cursor_pr.credentials import forge_credentials
from cursor_pr.source import ReviewError


@unittest.skipUnless(shutil.which("pr-agent-forge"), "the Go bridge ships in the image")
class ForgeHelperTests(unittest.TestCase):
    def test_helper_reports_transport_setup_failure_without_a_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Project(provider="gitlab", url="https://gitlab.example/org/repo", slug="org/repo",
                              checkout=root, requesters=frozenset({1}))
            env = {"HOME": str(root / "missing-home"), "PATH": os.environ["PATH"],
                   "REVIEW_GITLAB_TOKEN": "fixture", "REVIEW_GITLAB_LOCAL_ORIGIN": "https://127.0.0.1:4444",
                   "REVIEW_GITLAB_LOCAL_CA": str(root / "missing-ca")}
            with patch.dict(os.environ, env, clear=True), forge_credentials({project.key: project}):
                with self.assertRaisesRegex(ReviewError, "pr-agent-forge open exited 1: cannot read the GitLab proxy CA"):
                    discovery.open_changes(project)
            self.assertFalse((root / "missing-home").exists())

    def test_reads_and_posts_through_the_real_gitlab_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cert, key = root / "server.pem", root / "server.key"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                            "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
            project = Project(provider="gitlab", url="https://gitlab.example/prefix/org/repo",
                              slug="org/repo", checkout=root, requesters=frozenset({7}))
            original = "a" * 40
            change = dict(iid=9, state="opened", sha=original, changes_count="1", web_url=project.change_url(9),
                          title="Align acceptance criteria", description="Verify the expected state.")
            posts = []
            prefix = "/prefix/api/v4/projects/org%2Frepo"
            mr = prefix + "/merge_requests/9"

            class Handler(http.server.BaseHTTPRequestHandler):
                def send_json(self, status, value):
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(value).encode())

                def do_GET(self):
                    payloads = {
                        prefix: {"visibility": "private"},
                        prefix + "/merge_requests": [{"iid": 9}],
                        mr: change,
                        mr + "/diffs": [{"new_path": "app.py", "new_file": True, "diff": "@@ -0,0 +1 @@\n+x=1"}],
                        mr + "/notes": [{"id": 14, "body": "/review", "author": {"id": 7, "username": "requester"}}],
                    }
                    value = payloads.get(self.path.partition("?")[0])
                    if value is None or self.headers.get("PRIVATE-TOKEN") != "fixture":
                        self.send_json(404, {})
                        return
                    self.send_json(200, value)

                def do_POST(self):
                    if self.path != mr + "/notes" or self.headers.get("PRIVATE-TOKEN") != "fixture":
                        self.send_json(404, {})
                        return
                    posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["body"])
                    self.send_json(201, {"id": 21})

                def log_message(self, *args):
                    pass

            with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
                tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                tls.load_cert_chain(cert, key)
                server.socket = tls.wrap_socket(server.socket, server_side=True)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                env = {"HOME": str(root / "missing-home"), "PATH": os.environ["PATH"], "REVIEW_GITLAB_TOKEN": "fixture",
                       "REVIEW_GITLAB_LOCAL_ORIGIN": f"https://127.0.0.1:{server.server_port}",
                       "REVIEW_GITLAB_LOCAL_CA": str(cert)}
                try:
                    with patch.dict(os.environ, env, clear=True), forge_credentials({project.key: project}):
                        self.assertEqual(discovery.open_changes(project), [9])
                        self.assertEqual(discovery.comment_threads(project, (9,))[9][0]["databaseId"], 14)
                        reviewed = source.forge_change("org/repo#9", project=project)
                        self.assertEqual((reviewed.head, reviewed.files[0].path), (original, "app.py"))
                        self.assertEqual(reviewed.description, "")
                        described = source.forge_change("org/repo#9", project=project, with_description=True)
                        self.assertEqual((described.title, described.description),
                                         ("Align acceptance criteria", "Verify the expected state."))
                        self.assertEqual(described.head, original)
                        source.check_publication(project, 9, original, "Status text checked.")
                        source.post_comment(project, 9, original, "Review finished.", root)
                        source.post_comment(project, 9, original, "Review finished.", root)
                        change["sha"] = "b" * 40
                        with self.assertRaisesRegex(ReviewError, "differs from current head"):
                            source.post_comment(project, 9, original, "Review finished.", root)
                    self.assertEqual(posts, ["Review finished."])
                    self.assertFalse((root / "missing-home").exists())
                finally:
                    server.shutdown()
                    worker.join()
