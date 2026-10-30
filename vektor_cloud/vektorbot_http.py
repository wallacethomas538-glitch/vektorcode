"""HTTPS-only fetching with exact-host allowlists and pinned public DNS addresses."""
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self):
        return self.body.decode("utf-8", errors="replace")


def validate_url(url, allowed_hosts):
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if (p.scheme != "https" or not host or p.username is not None
            or p.password is not None or p.fragment or p.port not in (None, 443)):
        raise ValueError("Only credential-free HTTPS URLs on port 443, without fragments, are allowed")
    if host not in allowed_hosts:
        raise ValueError("Target host is not in the administrator allowlist")
    return p


def public_addresses(host):
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(
        host, 443, type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("DNS must resolve exclusively to public IP addresses")
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the validated address; preserve original hostname for TLS/SNI.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class SafeHTTP:
    def __init__(self, allowed_hosts, *, timeout=10, interval=1.0,
                 max_bytes=2_000_000, cancel=None, deadline=None):
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.timeout = timeout
        self.interval = max(1.0, interval)
        self.max_bytes = max_bytes
        self.cancel = cancel or threading.Event()
        self.deadline = deadline or (time.monotonic() + 300)
        self.last_request = 0.0

    def checkpoint(self):
        if self.cancel.is_set():
            raise InterruptedError("Task cancelled")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Task time budget exhausted")

    def request(self, url, *, method="GET", body=None, headers=None):
        self.checkpoint()
        p = validate_url(url, self.allowed_hosts)
        # Validate ALL DNS results, then pin one address to prevent DNS rebinding.
        address = public_addresses(p.hostname)[0]
        wait = max(0, self.interval - (time.monotonic() - self.last_request))
        if self.cancel.wait(wait):
            raise InterruptedError("Task cancelled")
        self.checkpoint()
        timeout = min(self.timeout, max(0.1, self.deadline - time.monotonic()))
        conn = PinnedHTTPS(p.hostname, address, timeout)
        self.last_request = time.monotonic()
        try:
            path = p.path or "/"
            if p.query:
                path += "?" + p.query
            request_headers = {"User-Agent": "VektorBots/0.1", "Accept-Encoding": "identity"}
            request_headers.update(headers or {})
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            if 300 <= response.status < 400:
                raise ValueError("Redirects are disabled; allowlist and use the final URL explicitly")
            chunks, size = [], 0
            while True:
                self.checkpoint()
                chunk = response.read1(min(65536, self.max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > self.max_bytes:
                    raise ValueError("Response exceeds configured byte limit")
            return Response(response.status, {k.lower(): v for k, v in response.getheaders()}, b"".join(chunks))
        finally:
            conn.close()
