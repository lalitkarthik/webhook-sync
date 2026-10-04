"""The in-process fakes served over real HTTP on 127.0.0.1, for a `wsync serve` subprocess (Q164).

respx reaches only the test's own process, so a crash test or load check serves FakeRazorpay
and FakeSheets here. Each request is handed to the fakes' own respx routes, on one event loop in
a thread of its own: the fakes answer exactly as they do in process, one request at a time
inside the fake, and a test scripts and reads them as it does there.

- Razorpay: point `sources.razorpay.api_url` at `url`; `/v1/...` goes to FakeRazorpay.
- Google: the server keeps calling `https://sheets.googleapis.com` and
  `https://oauth2.googleapis.com/token`. `environ` makes this server its HTTPS proxy and has it
  trust a certificate authority made up here, so the proxy answers for Google's two hosts (the
  server's HTTP clients honour `HTTPS_PROXY` and `SSL_CERT_FILE`). Nothing in the product
  changes for it; a tunnel to any other host is refused, and no certificate for one is trusted.

A fake's lost reply (`httpx.TimeoutException`) closes the connection unanswered.
"""

import asyncio
import functools
import ssl
import sys
import tempfile
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import TracebackType
from typing import Any, Protocol, Self, override

import httpx
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from fakes.razorpay import RAZORPAY_API

GOOGLE_HOSTS = ("sheets.googleapis.com", "oauth2.googleapis.com")
NOT_FORWARDED = ("host", "content-length", "connection", "transfer-encoding", "accept-encoding")
NOT_ANSWERED = ("content-length", "connection", "transfer-encoding", "content-encoding")


class Mountable(Protocol):
    def mount(self, router: respx.MockRouter) -> None: ...


class FakesOverHttp:
    """`fakes`, served on 127.0.0.1 at `url` while the block runs."""

    def __init__(self, *fakes: Mountable) -> None:
        router = respx.MockRouter(assert_all_called=False)
        for fake in fakes:
            fake.mount(router)
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(router.async_handler))
        self._loop = asyncio.new_event_loop()
        self._loop_thread = Thread(target=self._loop.run_forever, daemon=True)
        self._files = tempfile.TemporaryDirectory(prefix="wsync-fakes-")
        self.ca_file = Path(self._files.name) / "authority.pem"
        self._server = _Server(self)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> Self:
        authority, certificate, key = _certificates()
        self.ca_file.write_bytes(authority)
        chain = Path(self._files.name) / "google.pem"
        chain.write_bytes(certificate + key)
        self._server.tls.load_cert_chain(chain)
        self._loop_thread.start()
        Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        asyncio.run_coroutine_threadsafe(self._client.aclose(), self._loop).result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join()
        self._loop.close()
        self._files.cleanup()

    def environ(self, base: Mapping[str, str]) -> dict[str, str]:
        """`base`, its proxy variables dropped, with this server as the HTTPS proxy and its
        authority the only one trusted: what a `wsync serve` subprocess needs to reach Google
        here."""
        kept = {k: v for k, v in base.items() if not k.lower().endswith("_proxy")}
        return kept | {"HTTPS_PROXY": self.url, "SSL_CERT_FILE": str(self.ca_file)}

    def client(self) -> httpx.AsyncClient:
        """A client of this process that reaches Google's hosts here, as the server does."""
        verify = ssl.create_default_context(cafile=self.ca_file)
        return httpx.AsyncClient(proxy=self.url, verify=verify, trust_env=False)

    def answer(self, method: str, url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
        """The fakes' answer to one request, from their event loop."""
        call = self._client.request(method, url, headers=headers, content=body)
        return asyncio.run_coroutine_threadsafe(call, self._loop).result()


class _Server(ThreadingHTTPServer):
    def __init__(self, fakes: FakesOverHttp) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.fakes = fakes
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server  # pyright: ignore[reportIncompatibleVariableOverride]
    origin = RAZORPAY_API
    """Where a request on this connection goes: Razorpay, until a tunnel names Google's host."""

    def do_CONNECT(self) -> None:
        host = self.path.rpartition(":")[0]
        if host not in GOOGLE_HOSTS:
            self.send_error(403, f"only {', '.join(GOOGLE_HOSTS)} are faked")
            return
        self.send_response_only(200)
        self.end_headers()
        self.connection = self.server.tls.wrap_socket(self.connection, server_side=True)
        self.rfile = self.connection.makefile("rb")
        self.wfile = self.connection.makefile("wb")
        self.origin = f"https://{host}"

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        headers = {k: v for k, v in self.headers.items() if k.lower() not in NOT_FORWARDED}
        try:
            response = self.server.fakes.answer(
                self.command, self.origin + self.path, headers, body
            )
        except httpx.TimeoutException:
            self.close_connection = True  # the reply is lost
            return
        except AssertionError:  # respx: no fake has this route
            response = httpx.Response(404, json={"error": "not faked"})
        except Exception:
            traceback.print_exc()
            response = httpx.Response(500, json={"error": "the fake failed"})
        self.send_response_only(response.status_code)
        for name, value in response.headers.multi_items():
            if name.lower() not in NOT_ANSWERED:
                self.send_header(name, value)
        self.send_header("content-length", str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)

    @override
    def finish(self) -> None:
        super().finish()
        if isinstance(self.connection, ssl.SSLSocket):
            self.connection.close()

    @override
    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"fakes over http: {format % args}\n")


@functools.cache
def _certificates() -> tuple[bytes, bytes, bytes]:
    """A certificate authority made up here, and a certificate it signed for Google's two
    hosts with its key, all PEM. Valid from 2000 to 2100, whatever time a test moves to."""
    authority_key = ec.generate_private_key(ec.SECP256R1())
    key = ec.generate_private_key(ec.SECP256R1())
    authority_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wsync fake authority")])
    since, until = datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
    authority = (
        x509.CertificateBuilder()
        .subject_name(authority_name)
        .issuer_name(authority_name)
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(since)
        .not_valid_after(until)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(authority_key.public_key()), critical=False
        )
        .sign(authority_key, hashes.SHA256())
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, GOOGLE_HOSTS[0])]))
        .issuer_name(authority_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(since)
        .not_valid_after(until)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host) for host in GOOGLE_HOSTS]),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()),
            critical=False,
        )
        .sign(authority_key, hashes.SHA256())
    )
    return (
        authority.public_bytes(serialization.Encoding.PEM),
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
