"""Endpoint liveness checking for agent services."""
from __future__ import annotations

import ipaddress
import logging
import socket

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import config
from logger import AgentLogger
from models import EndpointCheck, LivenessResult

_log = logging.getLogger(__name__)


class LivenessChecker:
    """Check each declared service endpoint for liveness."""

    def __init__(self, logger: AgentLogger) -> None:
        self._logger = logger
        # Connection-pooled session for efficient endpoint checks
        self._session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=Retry(total=0),  # We handle retries ourselves
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def close(self) -> None:
        """Close the HTTP session."""
        self._session.close()

    def check(self, manifest: dict) -> LivenessResult:
        """Check all declared service endpoints."""
        services = manifest.get("services", [])
        if not services:
            return LivenessResult(
                success=True,
                endpoints_declared=0,
                endpoints_live=0,
                endpoints_secured=0,
                endpoints_dead=0,
                liveness_score=0,
                details=[],
            )

        # Fix #5: cap services to prevent DoS (1000 endpoints × 10s = hours blocked)
        services = services[:config.MAX_SERVICES_PER_MANIFEST]

        details = []
        for service in services:
            if not isinstance(service, dict):
                continue
            endpoint = service.get("endpoint", "")
            if not endpoint:
                details.append(EndpointCheck(endpoint="", status="missing", score=0))
                continue
            # Skip non-HTTP endpoints (e.g. stdio://)
            if not endpoint.startswith(("http://", "https://")):
                details.append(EndpointCheck(
                    endpoint=endpoint, status="non_http", http_code=0, score=50
                ))
                continue
            check = self._check_endpoint(endpoint)
            details.append(check)

        live = sum(1 for d in details if d.score > 0)
        secured = sum(1 for d in details if d.status == "secured")
        dead = sum(1 for d in details if d.score == 0)

        # Aggregate score: average of individual endpoint scores
        # Fix #4: use round() not // to avoid systematic underscoring
        total_score = sum(d.score for d in details)
        avg_score = round(total_score / len(details)) if details else 0

        return LivenessResult(
            success=True,
            endpoints_declared=len(services),
            endpoints_live=live,
            endpoints_secured=secured,
            endpoints_dead=dead,
            liveness_score=avg_score,
            details=details,
        )

    @staticmethod
    def _is_private_endpoint(endpoint: str) -> tuple[bool, bool]:
        """SSRF guard: block requests to private/internal IPs.

        Returns (is_private, dns_failed). DNS failures are not treated as
        private — the endpoint is simply unreachable.
        """
        from urllib.parse import urlparse
        parsed = urlparse(endpoint)
        hostname = parsed.hostname or ""
        if not hostname:
            return (True, False)
        try:
            resolved = socket.gethostbyname(hostname)
            ip = ipaddress.ip_address(resolved)
            is_private = (
                ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved
            )
            return (is_private, False)
        except Exception:
            return (False, True)  # DNS failure = not private, just unreachable

    def _check_endpoint(self, endpoint: str) -> EndpointCheck:
        """Check a single endpoint. Interpret status codes correctly."""
        # SSRF guard: block private/internal IPs
        is_private, dns_failed = self._is_private_endpoint(endpoint)
        if is_private:
            _log.warning("Blocked liveness check to private/internal endpoint: %s", endpoint)
            return EndpointCheck(endpoint=endpoint, status="blocked_private", http_code=0, score=0)
        if dns_failed:
            return EndpointCheck(endpoint=endpoint, status="unreachable", http_code=0, score=0)

        try:
            # Use HEAD request first (lighter), fall back to GET if 405
            resp = self._session.head(
                endpoint, timeout=config.LIVENESS_TIMEOUT, allow_redirects=False
            )
            resp.close()
            status = resp.status_code
            if status == 405:
                # Method Not Allowed — try GET instead
                resp = self._session.get(
                    endpoint, timeout=config.LIVENESS_TIMEOUT, allow_redirects=False
                )
                resp.close()
                status = resp.status_code
        except requests.Timeout:
            return EndpointCheck(endpoint=endpoint, status="timeout", http_code=0, score=0)
        except requests.ConnectionError:
            return EndpointCheck(endpoint=endpoint, status="unreachable", http_code=0, score=0)
        except requests.RequestException:
            return EndpointCheck(endpoint=endpoint, status="error", http_code=0, score=0)

        # Status code interpretation
        if status in (200, 201, 204):
            return EndpointCheck(endpoint=endpoint, status="alive", http_code=status, score=100)
        if status in (401, 403):
            # Endpoint exists AND is properly secured -- this is GOOD
            return EndpointCheck(endpoint=endpoint, status="secured", http_code=status, score=100)
        if status in (301, 302, 307, 308):
            return EndpointCheck(endpoint=endpoint, status="redirect", http_code=status, score=80)
        if status == 404:
            return EndpointCheck(endpoint=endpoint, status="not_found", http_code=status, score=0)
        if status in (500, 502, 503, 504):
            return EndpointCheck(endpoint=endpoint, status="server_error", http_code=status, score=20)
        if status == 429:
            # Rate limited -- endpoint exists and is active
            return EndpointCheck(endpoint=endpoint, status="rate_limited", http_code=status, score=90)
        # Unknown status
        return EndpointCheck(endpoint=endpoint, status=f"http_{status}", http_code=status, score=50)
