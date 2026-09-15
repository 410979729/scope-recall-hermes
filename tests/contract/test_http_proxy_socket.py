"""Real loopback CONNECT/TLS tests for the bounded HTTPS worker."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import select
import socket
import ssl
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from scope_recall.runtime import _http_worker as worker


def _payload(url: str, *, body: bytes = b"proxy-body") -> bytes:
    return json.dumps(
        {
            "url": url,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "headers": {"X-Test": "loopback"},
            "timeout_seconds": 5.0,
            "max_response_bytes": 1024,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _cert_material(tmp_path: Path) -> tuple[Path, Path, Path]:
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TEST Scope Recall CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tmp_path / "test-ca.pem"
    cert_path = tmp_path / "test-server.pem"
    key_path = tmp_path / "test-server-key.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


class _TLSTarget:
    def __init__(self, cert_path: Path, key_path: Path) -> None:
        self.cert_path = cert_path
        self.key_path = key_path
        self.ready = threading.Event()
        self.done = threading.Event()
        self.port = 0
        self.headers: dict[str, str] = {}
        self.body = b""
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self.ready.wait(3)

    def join(self) -> None:
        assert self.done.wait(5)
        self._thread.join(timeout=1)
        if self.error:
            raise self.error

    def _run(self) -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.port = listener.getsockname()[1]
        self.ready.set()
        try:
            raw, _ = listener.accept()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(self.cert_path), str(self.key_path))
            with context.wrap_socket(raw, server_side=True) as conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(4096)
                header_blob, remainder = request.split(b"\r\n\r\n", 1)
                lines = header_blob.decode("iso-8859-1").split("\r\n")
                for line in lines[1:]:
                    key, value = line.split(":", 1)
                    self.headers[key.lower()] = value.strip()
                length = int(self.headers.get("content-length", "0"))
                while len(remainder) < length:
                    remainder += conn.recv(4096)
                self.body = remainder[:length]
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"Connection: close\r\n\r\n"
                    b"5\r\nhello\r\n"
                    b"6\r\n world\r\n"
                    b"0\r\nX-Test-Trailer: yes\r\n\r\n"
                )
        except BaseException as exc:  # surfaced by join
            self.error = exc
        finally:
            listener.close()
            self.done.set()


class _ConnectProxy:
    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.ready = threading.Event()
        self.done = threading.Event()
        self.port = 0
        self.connect_line = ""
        self.headers: dict[str, str] = {}
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self.ready.wait(3)

    def join(self) -> None:
        assert self.done.wait(5)
        self._thread.join(timeout=1)
        if self.error:
            raise self.error

    def _run(self) -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.port = listener.getsockname()[1]
        self.ready.set()
        client = target = None
        try:
            client, _ = listener.accept()
            header_blob = b""
            while b"\r\n\r\n" not in header_blob:
                header_blob += client.recv(4096)
            header_blob, _ = header_blob.split(b"\r\n\r\n", 1)
            lines = header_blob.decode("iso-8859-1").split("\r\n")
            self.connect_line = lines[0]
            for line in lines[1:]:
                key, value = line.split(":", 1)
                self.headers[key.lower()] = value.strip()
            target = socket.create_connection(("127.0.0.1", self.target_port), timeout=3)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            while True:
                readable, _, _ = select.select([client, target], [], [], 3)
                if not readable:
                    raise TimeoutError("proxy relay idle")
                for source in readable:
                    data = source.recv(65_536)
                    if not data:
                        return
                    (target if source is client else client).sendall(data)
        except BaseException as exc:  # surfaced by join
            self.error = exc
        finally:
            if client is not None:
                client.close()
            if target is not None:
                target.close()
            listener.close()
            self.done.set()


def _trusted_client_context(ca_path: Path) -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(ca_path))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    return context


def test_real_connect_proxy_tls_chunked_and_auth_isolation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ca_path, cert_path, key_path = _cert_material(tmp_path)
    target = _TLSTarget(cert_path, key_path)
    target.start()
    proxy = _ConnectProxy(target.port)
    proxy.start()
    client_context = _trusted_client_context(ca_path)
    monkeypatch.setattr(worker.ssl, "create_default_context", lambda: client_context)
    monkeypatch.setattr(
        worker.urllib.request,
        "getproxies",
        lambda: {"https": f"http://proxy-user:proxy-pass@127.0.0.1:{proxy.port}"},
    )
    monkeypatch.setattr(worker.urllib.request, "proxy_bypass", lambda _host: False)

    result = json.loads(worker._request(_payload(f"https://127.0.0.1:{target.port}/answer")).decode())
    proxy.join()
    target.join()

    assert result["ok"] is True
    assert base64.b64decode(result["body_b64"]) == b"hello world"
    # The tunnel target is what matters here. CPython's http.client builds its
    # CONNECT line with a hardcoded HTTP/1.0 regardless of _http_vsn_str, so
    # pinning the version string asserted stdlib trivia and failed on the
    # interpreter rather than on anything this code does.
    assert proxy.connect_line.startswith(f"CONNECT 127.0.0.1:{target.port} HTTP/1.")
    assert proxy.headers["proxy-authorization"].startswith("Basic ")
    assert "proxy-authorization" not in target.headers
    assert target.headers["x-test"] == "loopback"
    assert target.body == b"proxy-body"


def test_real_no_proxy_bypasses_bad_proxy_and_reaches_tls_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ca_path, cert_path, key_path = _cert_material(tmp_path)
    target = _TLSTarget(cert_path, key_path)
    target.start()
    client_context = _trusted_client_context(ca_path)
    monkeypatch.setattr(worker.ssl, "create_default_context", lambda: client_context)
    monkeypatch.setattr(worker.urllib.request, "getproxies", lambda: {"https": "socks5://bad.invalid:9"})
    monkeypatch.setattr(worker.urllib.request, "proxy_bypass", lambda host: host == "127.0.0.1")

    result = json.loads(worker._request(_payload(f"https://127.0.0.1:{target.port}/direct")).decode())
    target.join()

    assert result["ok"] is True
    assert base64.b64decode(result["body_b64"]) == b"hello world"
    assert target.headers["x-test"] == "loopback"
