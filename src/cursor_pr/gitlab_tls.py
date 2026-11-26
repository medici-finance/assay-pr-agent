"""Instance-scoped GitLab trust for Python, Assay's Go clients and git-sync."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import time
from urllib.parse import urlsplit

CA = "REVIEW_GITLAB_CA"
CRL = "REVIEW_GITLAB_CRL"
NAMES = {CA: "gitlab-ca.pem", CRL: "gitlab-crl.pem"}


def validate(name, pem):
    label = "CERTIFICATE" if name == CA else "X509 CRL"
    block = rf"-----BEGIN {label}-----\s+[A-Za-z0-9+/=\s]+-----END {label}-----"
    if not re.fullmatch(rf"(?:{block}\s*)+", pem.strip()):
        raise ValueError(f"Expected PEM {label} data")
    return pem.strip()


def context(directory):
    ctx = ssl.create_default_context()
    for name, filename in NAMES.items():
        path = directory / filename
        if path.exists():
            ctx.load_verify_locations(cafile=str(path))
            if name == CRL:
                ctx.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    return ctx


@contextmanager
def transport(base, directory):
    # Go's standard verifier does not consult CRLs. Terminate its upstream TLS
    # with HAProxy/OpenSSL, validating the actual connection carrying the token.
    # The loopback leg also uses TLS with an ephemeral, locally generated key.
    from .config import gitlab_base
    site = urlsplit(gitlab_base(base))
    ca = directory / NAMES[CA]
    crl = directory / NAMES[CRL]
    if not ca.exists() and not crl.exists():
        yield {}
        return
    public_roots = Path(ssl.get_default_verify_paths().cafile).read_bytes()
    roots = directory / "upstream-roots.pem"
    roots.write_bytes(public_roots + (b"\n" + ca.read_bytes() if ca.exists() else b""))
    cert, key = directory / "listener.crt", directory / "listener.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-nodes", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-days", "3650", "-out", str(cert), "-keyout", str(key)], check=True, capture_output=True)
    identity = directory / "listener.pem"
    identity.write_bytes(cert.read_bytes() + key.read_bytes())
    identity.chmod(0o600)
    local_ca = directory / "client-roots.pem"
    local_ca.write_bytes(public_roots + b"\n" + cert.read_bytes())
    revocations = f" crl-file {crl}" if crl.exists() else ""
    host = site.hostname
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config = directory / "haproxy.cfg"
        config.write_text(f"""defaults
    mode http
    timeout connect 10s
    timeout client 10m
    timeout server 10m
frontend local
    bind fd@{listener.fileno()} ssl crt {identity}
    default_backend gitlab
backend gitlab
    http-request set-header Host {site.netloc}
    http-response deny if {{ status 300:399 }}
    server instance {host}:{site.port or 443} ssl sni str({host}) verify required verifyhost {host} ca-file {roots}{revocations}
""")
        process = subprocess.Popen(["haproxy", "-db", "-f", str(config)], pass_fds=(listener.fileno(),),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            listener.close()
            local = ssl.create_default_context(cafile=str(cert))
            for _ in range(50):
                if process.poll() is not None:
                    raise ValueError("GitLab TLS transport could not start. Check the base URL, CA and CRL.")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2) as conn:
                        with local.wrap_socket(conn, server_hostname="127.0.0.1"):
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise ValueError("GitLab TLS transport did not become ready")
            yield {"REVIEW_GITLAB_LOCAL_ORIGIN": f"https://127.0.0.1:{port}",
                   "REVIEW_GITLAB_LOCAL_CA": str(local_ca)}
        finally:
            process.terminate()
            process.wait(timeout=10)


def client_options():
    if "REVIEW_GITLAB_LOCAL_CA" not in os.environ:
        return {}
    return {"verify": context(Path(os.environ["ASSAY_CONFIG_HOME"]))}


def local_url(url):
    target = urlsplit(url)
    return os.environ["REVIEW_GITLAB_LOCAL_ORIGIN"] + target.path
