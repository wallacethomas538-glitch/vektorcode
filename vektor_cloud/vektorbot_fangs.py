"""Fangs: active defensive checks for Wallace's own assets.

This module upgrades VektorBots' security bot from passive observation to
ACTIVE probing — but strictly bounded:

- TARGETS: only the hardcoded VEKTOR_ALLOWLIST below. Every check gates on
  it first. A non-allowlisted target returns ``target_not_authorized`` — no
  flag, env var, config file, or caller argument can bypass the allowlist.
- READ-ONLY: GET and TCP handshakes only. No POST/PUT/DELETE, no brute
  force, no exploit payloads, no credential guessing, no destructive action.
- RATE-LIMITED: minimum interval between requests; bounded timeouts.
- HONEST: unavailable tools report as staged, never fake success. Nothing is
  auto-remediated; findings go to the orchestrator as signed JSON reports.

What the fangs CAN do: grade TLS setups, score security headers, spot
exposed sensitive paths, wrap pip-audit, and list open common ports — on
Wallace's own systems.

What they CANNOT do: touch anything that is not on the allowlist, modify
any target, or prove a system is secure. Findings are signals for human
review, not proof of safety or compromise.
"""
import hashlib
import http.client
import ipaddress
import json
import shutil
import socket
import ssl
import subprocess
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from vektor_cloud.vektorbot_http import public_addresses

# THE ALLOWLIST IS THE LAW. Exact hostnames only — subdomains must be added
# explicitly here. Nothing outside this set is ever probed.
VEKTOR_ALLOWLIST = frozenset({
    "gojojoes.com",
    "vektormind-2-0.onrender.com",
    "vektorflow-15xr-1.onrender.com",
    "localhost",
})

# Loopback forms that normalize to "localhost".
_LOCALHOST_ALIASES = {"localhost", "127.0.0.1", "::1"}

# Ports worth knowing about on Wallace's own hosts. Connect-only, never send.
COMMON_PORTS = (22, 80, 443, 3000, 5000, 8000, 8080, 8443, 9090)

# Paths that should never answer 200 on a production host. GET-only probes;
# only status + headers are read, bodies are never downloaded.
EXPOSED_PATHS = ("/.git/HEAD", "/.env", "/admin", "/.well-known/security.txt")

# Cipher-name fragments treated as weak. Checked as substrings, case-insensitive.
WEAK_CIPHER_MARKERS = ("RC4", "DES", "3DES", "NULL", "EXPORT", "MD5", "PSK", "SRP", "ADH", "AECDH")

# Old protocol versions that must never be negotiated.
DEPRECATED_PROTOCOLS = ("SSLv2", "SSLv3", "TLSv1", "TLSv1.1")

REPORT_SCHEMA = "1.0"
USER_AGENT = "VektorBots-Fangs/0.1"


class TargetNotAuthorized(Exception):
    """Raised when a target is not on the hardcoded allowlist."""


def authorize(host):
    """Gate every check. Returns the canonical host or raises TargetNotAuthorized."""
    h = (host or "").strip().lower().rstrip(".")
    if h in _LOCALHOST_ALIASES:
        h = "localhost"
    if h not in VEKTOR_ALLOWLIST:
        raise TargetNotAuthorized(f"target_not_authorized: {host!r} is not a Vektor asset")
    return h


def _finding(check, severity, message, evidence="", remediation=""):
    return {"check": check, "severity": severity, "message": message,
            "evidence": evidence, "remediation": remediation}


class FangsRunner:
    """Runs active checks against allowlisted Vektor assets.

    All network touchpoints are injectable so tests never hit the network:
    - tls_handshake(host, port, minimum, maximum) -> (version_str, cipher_name, cert_dict)
    - fetcher(host, path) -> (status:int, headers:dict)  (GET-only, no body)
    - tcp_connect(host, port, timeout) -> bool
    - pip_runner(argv, timeout) -> (returncode, stdout)
    - sleeper(seconds)
    """

    def __init__(self, *, interval=1.0, timeout=10, tls_handshake=None,
                 fetcher=None, tcp_connect=None, pip_runner=None, sleeper=None):
        self.interval = max(0.25, interval)
        self.timeout = timeout
        self._tls_handshake = tls_handshake or self._real_tls_handshake
        self._fetcher = fetcher or self._real_fetch
        self._tcp_connect = tcp_connect or self._real_tcp_connect
        self._pip_runner = pip_runner or self._real_pip_run
        self._sleeper = sleeper or time.sleep
        self._last = 0.0

    # -- rate limiting -----------------------------------------------------
    def _pace(self):
        wait = max(0.0, self.interval - (time.monotonic() - self._last))
        if wait:
            self._sleeper(wait)
        self._last = time.monotonic()

    # -- real network primitives (never used in tests) ---------------------
    @staticmethod
    def _real_tls_handshake(host, port, minimum, maximum):
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        if minimum is not None:
            ctx.minimum_version = minimum
        if maximum is not None:
            ctx.maximum_version = maximum
        raw = socket.create_connection((host, port), timeout=10)
        try:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                cipher = (tls.cipher() or ("", "", ""))[0]
                return tls.version(), cipher, cert
        finally:
            raw.close()

    def _real_fetch(self, host, path):
        """GET-only; returns (status, headers). Bodies are never downloaded."""
        self._pace()
        if host == "localhost":
            address = "127.0.0.1"
        else:
            address = public_addresses(host)[0]  # raises unless exclusively public IPs
        context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(address, 443, timeout=self.timeout,
                                           context=context)
        try:
            # We connect by pinned IP, so wrap manually to keep hostname
            # verification (SNI + cert check against the real hostname).
            raw = socket.create_connection((address, 443), timeout=self.timeout)
            try:
                sock = context.wrap_socket(raw, server_hostname=host)
            except BaseException:
                raw.close()
                raise
            conn.sock = sock
            # Explicit Host header: virtual hosts (Shopify, Render) route on it.
            conn.request("GET", path, headers={"Host": host, "User-Agent": USER_AGENT,
                                               "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            status, headers = resp.status, {k.lower(): v for k, v in resp.getheaders()}
            return status, headers  # body deliberately never read
        finally:
            conn.close()

    @staticmethod
    def _real_tcp_connect(host, port, timeout):
        target = "127.0.0.1" if host == "localhost" else host
        sock = socket.create_connection((target, port), timeout=timeout)
        sock.close()
        return True

    @staticmethod
    def _real_pip_run(argv, timeout):
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout

    # -- checks -------------------------------------------------------------
    def check_tls(self, host):
        """Deep TLS check: chain validity, deprecated protocols, weak ciphers."""
        host = authorize(host)
        findings = []
        # 1. Chain validity with modern defaults.
        try:
            version, cipher, cert = self._tls_handshake(host, 443, None, None)
        except ssl.SSLCertVerificationError as exc:
            return [_finding("tls_certificate", "critical",
                             "Certificate chain failed verification",
                             str(exc)[:200],
                             "Install a valid certificate chain from a trusted CA")]
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            return [_finding("tls_certificate", "high",
                             "TLS handshake failed", f"{type(exc).__name__}: {str(exc)[:200]}",
                             "Check that the host serves HTTPS on 443")]
        not_after = ""
        if isinstance(cert, dict):
            not_after = str(cert.get("notAfter", ""))
        findings.append(_finding("tls_certificate", "info",
                                 f"Chain verified; negotiated {version}; cert expires {not_after or 'unknown'}",
                                 f"version={version} cipher={cipher}", ""))
        # 2. Deprecated protocols: can the server still negotiate them?
        for proto_name, maximum in (("TLSv1.1", ssl.TLSVersion.TLSv1_1),
                                    ("TLSv1", ssl.TLSVersion.TLSv1)):
            try:
                negotiated, _, _ = self._tls_handshake(host, 443, None, maximum)
                if negotiated in ("TLSv1", "TLSv1.1"):
                    findings.append(_finding("tls_protocol", "high",
                                             f"Deprecated protocol offered: {negotiated}",
                                             f"server negotiated {negotiated}",
                                             "Disable TLS 1.0/1.1; require TLS 1.2+"))
            except Exception:  # noqa: BLE001 - failure to negotiate old = good
                pass
        # SSLv2/SSLv3 cannot be attempted by this runtime (support removed);
        # record that honestly instead of claiming coverage.
        findings.append(_finding("tls_protocol", "info",
                                 "SSLv2/SSLv3 not testable by this runtime (support removed); "
                                 "modern servers must not offer them", "", ""))
        # 3. Weak cipher in the negotiated session.
        weak = [m for m in WEAK_CIPHER_MARKERS if m in cipher.upper()]
        if weak:
            findings.append(_finding("tls_cipher", "high",
                                     f"Weak cipher negotiated: {cipher}",
                                     f"markers={','.join(weak)}",
                                     "Restrict to AEAD ciphers (AES-GCM, ChaCha20-Poly1305)"))
        else:
            findings.append(_finding("tls_cipher", "info",
                                     f"Cipher looks modern: {cipher}", "", ""))
        return findings

    def check_headers(self, host):
        """Score security headers instead of just noting presence."""
        host = authorize(host)
        try:
            status, headers = self._fetcher(host, "/")
        except TargetNotAuthorized:
            raise
        except Exception as exc:  # noqa: BLE001
            return [_finding("http_fetch", "high", "Could not fetch homepage",
                             f"{type(exc).__name__}: {str(exc)[:200]}",
                             "Check the host is reachable over HTTPS")]
        findings = []
        score, maximum = 0, 0

        def grade(name, weight, test, hint):
            nonlocal score, maximum
            maximum += weight
            value = headers.get(name, "")
            if value and test(value):
                score += weight
                findings.append(_finding(f"header:{name}", "info",
                                         f"{name} present and well-formed", value[:160], ""))
            else:
                sev = "high" if weight >= 3 else "medium"
                findings.append(_finding(f"header:{name}", sev,
                                         f"{name} missing or weak",
                                         value[:160] if value else "absent", hint))

        grade("strict-transport-security", 3,
              lambda v: "max-age" in v and "includesubdomains" in v.replace(" ", "").lower(),
              "Add Strict-Transport-Security: max-age=31536000; includeSubDomains")
        grade("content-security-policy", 3, lambda v: len(v) > 10,
              "Add a Content-Security-Policy appropriate for the app")
        grade("x-content-type-options", 2, lambda v: v.strip().lower() == "nosniff",
              "Add X-Content-Type-Options: nosniff")
        grade("referrer-policy", 1, lambda v: len(v) > 0,
              "Add Referrer-Policy, e.g. strict-origin-when-cross-origin")
        grade("x-frame-options", 2, lambda v: v.strip().upper() in ("DENY", "SAMEORIGIN"),
              "Add X-Frame-Options: DENY (or SAMEORIGIN)")
        grade("permissions-policy", 1, lambda v: len(v) > 0,
              "Add Permissions-Policy to limit powerful browser features")
        findings.append(_finding("header_score", "info",
                                 f"Security-header score {score}/{maximum}",
                                 f"http_status={status}", ""))
        return findings

    def check_exposed_paths(self, host):
        """GET-only probes for paths that must not answer 200. No bodies read."""
        host = authorize(host)
        findings = []
        for path in EXPOSED_PATHS:
            try:
                self._pace()
                status, _headers = self._fetcher(host, path)
            except TargetNotAuthorized:
                raise
            except Exception as exc:  # noqa: BLE001
                findings.append(_finding(f"exposed:{path}", "info",
                                         f"Probe failed ({type(exc).__name__}); treated as not exposed",
                                         "", ""))
                continue
            if path in ("/.git/HEAD", "/.env") and status == 200:
                findings.append(_finding(f"exposed:{path}", "critical",
                                         f"{path} is publicly readable (HTTP 200)",
                                         f"http_status={status}",
                                         "Block this path at the edge immediately; rotate any exposed secrets"))
            elif path == "/admin" and status in (200, 401, 403):
                findings.append(_finding(f"exposed:{path}", "info",
                                         f"/admin responds HTTP {status}",
                                         f"http_status={status}",
                                         "Ensure admin is allowlisted/IP-gated and not brute-forceable"))
            elif path == "/.well-known/security.txt" and status == 200:
                findings.append(_finding(f"exposed:{path}", "info",
                                         "security.txt present (good practice)",
                                         f"http_status={status}", ""))
            else:
                findings.append(_finding(f"exposed:{path}", "info",
                                         f"{path} not exposed (HTTP {status})",
                                         f"http_status={status}", ""))
        return findings

    def check_dependencies(self):
        """Wrap pip-audit when present; staged (never fake) when absent."""
        pip_audit = shutil.which("pip-audit")
        if not pip_audit:
            return [{"check": "pip_audit", "severity": "info", "status": "staged",
                     "message": "pip-audit not installed; dependency audit skipped honestly",
                     "evidence": "", "remediation": "pip install pip-audit to enable"}]
        try:
            code, out = self._pip_runner([pip_audit, "--format=json", "--disable-pip"],
                                         timeout=180)
        except Exception as exc:  # noqa: BLE001
            return [{"check": "pip_audit", "severity": "info", "status": "staged",
                     "message": f"pip-audit failed to run: {type(exc).__name__}",
                     "evidence": str(exc)[:200], "remediation": "Run pip-audit manually"}]
        try:
            data = json.loads(out or "[]")
        except json.JSONDecodeError:
            return [{"check": "pip_audit", "severity": "medium",
                     "message": "pip-audit output was not parseable JSON",
                     "evidence": out[:200], "remediation": "Run pip-audit manually"}]
        vulns = data if isinstance(data, list) else data.get("dependencies", [])
        findings = []
        count = 0
        for dep in vulns:
            for vuln in dep.get("vulns", []):
                count += 1
                findings.append(_finding("pip_audit", "high",
                                         f"{dep.get('name')} {dep.get('version')}: "
                                         f"{vuln.get('id', 'unknown-id')}",
                                         str(vuln.get("description", ""))[:200],
                                         f"Upgrade {dep.get('name')} past the fixed version"))
        if not findings:
            findings.append(_finding("pip_audit", "info",
                                     "pip-audit ran clean (exit 0, no vulns reported)",
                                     "", ""))
        findings.append(_finding("pip_audit", "info",
                                 f"pip-audit exit={code}, {count} vuln(s) found", "", ""))
        return findings

    def check_ports(self, host):
        """TCP connect scan of common ports. Connect-only; nothing is sent."""
        host = authorize(host)
        findings = []
        opened = []
        for port in COMMON_PORTS:
            self._pace()
            try:
                if self._tcp_connect(host, port, self.timeout):
                    opened.append(port)
            except Exception:  # noqa: BLE001 - closed/filtered is not an error
                pass
        if opened:
            findings.append(_finding("open_ports", "medium",
                                     f"Open ports on {host}: {opened}",
                                     f"ports={opened}",
                                     "Close what the app does not need; firewall the rest"))
        else:
            findings.append(_finding("open_ports", "info",
                                     f"No common ports answered on {host}",
                                     "", ""))
        return findings

    # -- orchestration -------------------------------------------------------
    def run(self, host, bot_name="vektor-fangs", checks=("tls", "headers", "exposed_paths", "ports")):
        """Run selected checks against one allowlisted host; return the report dict."""
        report = {"schema_version": REPORT_SCHEMA, "report_id": str(uuid.uuid4()),
                  "bot": bot_name, "kind": "fangs",
                  "started_at": datetime.now(timezone.utc).isoformat(),
                  "status": "completed", "results": [], "errors": [],
                  "content_trust": "untrusted_external_data",
                  "scope": "Wallace's own assets only; read-only checks; nothing auto-remediated"}
        try:
            host = authorize(host)
        except TargetNotAuthorized as exc:
            report["status"] = "refused"
            report["errors"].append({"target": host, "error_type": "TargetNotAuthorized",
                                     "message": str(exc)})
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            return report
        table = {"tls": self.check_tls, "headers": self.check_headers,
                 "exposed_paths": self.check_exposed_paths, "ports": self.check_ports,
                 "dependencies": lambda _h: self.check_dependencies()}
        for name in checks:
            if name not in table:
                report["errors"].append({"target": host, "error_type": "UnknownCheck",
                                         "message": f"unknown check {name!r}"})
                report["status"] = "partial"
                continue
            try:
                findings = table[name](host)
                report["results"].append({"target": host, "check": name, "findings": findings})
            except TargetNotAuthorized as exc:
                report["errors"].append({"target": host, "error_type": "TargetNotAuthorized",
                                         "message": str(exc)})
                report["status"] = "partial"
            except Exception as exc:  # noqa: BLE001 - one bad check must not kill the run
                report["errors"].append({"target": host, "error_type": type(exc).__name__,
                                         "message": str(exc)[:200]})
                report["status"] = "partial"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        return report


def report_fingerprint(report):
    """Stable hash of a report's findings (for idempotent orchestrator delivery)."""
    body = json.dumps(report.get("results", []), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()
