import http.server
import os
import socket
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from cursor_pr import gitlab_tls, source, sync
from cursor_pr.config import project_from
from cursor_pr.credentials import forge_credentials


class GitLabTrustTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("haproxy"), "exercises the container TLS transport")
    def test_private_instance_with_current_then_revoked_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/"index").touch()
            (root/"serial").write_text("0100")
            (root/"ca.conf").write_text("""[ca]
default_ca=issuer
[issuer]
database=index
new_certs_dir=.
certificate=gitlab-ca.pem
private_key=ca.key
serial=serial
default_md=sha256
default_days=10
default_crl_days=10
policy=policy
[policy]
commonName=supplied
[server]
subjectAltName=DNS:localhost
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
authorityKeyIdentifier=keyid
""")

            def openssl(*args):
                subprocess.run(["openssl", *args], cwd=root, check=True, capture_output=True)

            openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "10", "-subj", "/CN=fixture",
                    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                    "-keyout", "ca.key", "-out", "gitlab-ca.pem")
            openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
                    "-keyout", "server.key", "-out", "server.csr")
            openssl("ca", "-batch", "-config", "ca.conf", "-extensions", "server", "-in", "server.csr", "-out", "server.crt")
            seen = []

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    seen.append((self.headers["Host"], self.path))
                    self.send_response(200)
                    self.end_headers()

                def log_message(self, *args):
                    pass

            class DualStack(http.server.ThreadingHTTPServer):
                address_family = socket.AF_INET6

            with DualStack(("::", 0), Handler) as server:
                tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                tls.load_cert_chain(root/"server.crt", root/"server.key")
                server.socket = tls.wrap_socket(server.socket, server_side=True)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                host = f"localhost:{server.server_port}"
                site = "https://" + host + "/gitlab"
                try:
                    for revoked in (False, True):
                        if revoked:
                            openssl("ca", "-config", "ca.conf", "-revoke", "server.crt")
                        openssl("ca", "-config", "ca.conf", "-gencrl", "-out", "gitlab-crl.pem")
                        ctx = gitlab_tls.context(root)
                        if revoked:
                            with self.assertRaises(httpx.ConnectError):
                                httpx.get(site, verify=ctx)
                        else:
                            self.assertEqual(httpx.get(site, verify=ctx).status_code, 200)
                        with gitlab_tls.transport(site + "/api/v4", root) as env:
                            url = env["REVIEW_GITLAB_LOCAL_ORIGIN"] + "/gitlab"
                            local = ssl.create_default_context(cafile=env["REVIEW_GITLAB_LOCAL_CA"])
                            self.assertEqual((root/"listener.pem").stat().st_mode & 0o777, 0o600)
                            code = httpx.get(url, verify=local).status_code
                            self.assertEqual(code, 503 if revoked else 200)
                        # Exercise the actual binaries' TLS and URL routing. A fixture HTTP response
                        # intentionally contains no git objects or MR metadata.
                        row = dict(provider="gitlab", gitlab_base=site, url=site + "/org/project",
                                   managed=True, requester_ids=[11])
                        project = project_from(row)
                        env = {"HOME": str(root), "PATH": os.environ["PATH"], "REVIEW_OPERATOR": "owner:11",
                               "REVIEW_GITLAB_TOKEN": "test-only", "REVIEW_GITLAB_CA_FILE": str(root/"gitlab-ca.pem"),
                               "REVIEW_GITLAB_CRL_FILE": str(root/"gitlab-crl.pem")}
                        before = len(seen)
                        with patch.dict(os.environ, env, clear=True), forge_credentials({"gl": project}, root):
                            try:
                                source.run_desk(["deskread", "change", "--change", "org/project#1"], cwd=root)
                            except source.ReviewError:
                                pass
                            paths = [path for _, path in seen[before:]]
                            if not revoked:
                                self.assertTrue(any("/gitlab/api/v4/projects/" in path for path in paths), paths)
                            try:
                                password = source.reviewer_token_file("gitlab", "org/project", root)
                                sync.sync_ref("gitlab", project.url, root / "checkout", "HEAD", 1, password, root)
                            except subprocess.CalledProcessError:
                                pass
                        if revoked:
                            self.assertEqual(len(seen), before)
                        else:
                            self.assertTrue(any("/gitlab/org/project.git/" in path for _, path in seen[before:]))
                    self.assertTrue(all(h == host for h, _ in seen))
                finally:
                    server.shutdown()
