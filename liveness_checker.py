"""Endpoint liveness checking for agent services."""
from __future__ import annotations

import ipaddress
import logging
import socket
import time

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

        # MCP protocol compliance check for stdio:// or SSE endpoints
        protocol_score = self._check_protocol_compliance(services)

        return LivenessResult(
            success=True,
            endpoints_declared=len(services),
            endpoints_live=live,
            endpoints_secured=secured,
            endpoints_dead=dead,
            liveness_score=avg_score,
            protocol_compliance_score=protocol_score,
            details=details,
        )

    def _check_protocol_compliance(self, services: list[dict]) -> int:
        """Check MCP protocol declaration in manifest services.

        This checks whether services declare MCP-compatible transport or metadata.
        It does NOT perform an actual MCP initialize handshake.

        Scoring:
        - 100: Agent declares MCP transport (stdio, SSE) and has live HTTP endpoints
        - 90: Agent declares MCP transport (no live HTTP)
        - 80: Agent declares MCP metadata (supportedProtocols, mcpVersion)
        - 50: Agent has HTTP endpoints only (no MCP signals)
        - 0: No protocol signals detected
        """
        score = 0
        has_mcp_transport = False
        has_mcp_metadata = False
        has_live_http = False

        for svc in services:
            if not isinstance(svc, dict):
                continue

            endpoint = svc.get("endpoint", "")
            transport = svc.get("transport", "").lower()
            protocol = svc.get("protocol", "").lower()

            # Check for MCP transport declarations
            if transport in ("stdio", "sse", "streamable-http"):
                has_mcp_transport = True
            if "mcp" in protocol or "mcp" in svc.get("type", "").lower():
                has_mcp_metadata = True

            # Check for MCP-related fields
            if svc.get("mcpVersion") or svc.get("supportedProtocols"):
                has_mcp_metadata = True

            # Check for live HTTP endpoints (already verified by liveness check)
            if isinstance(endpoint, str) and endpoint.startswith(("http://", "https://")):
                has_live_http = True

            # Check for SSE endpoint pattern
            if isinstance(endpoint, str) and "/sse" in endpoint.lower():
                has_mcp_transport = True

        if has_mcp_transport and has_live_http:
            score = 100
        elif has_mcp_transport:
            score = 90
        elif has_mcp_metadata:
            score = 80
        elif has_live_http:
            # HTTP endpoints exist but no MCP signals — partial compliance
            score = 50
        # else: 0

        _log.info("Protocol compliance: score=%d mcp_transport=%s mcp_meta=%s http=%s",
                   score, has_mcp_transport, has_mcp_metadata, has_live_http)
        return score

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

    @staticmethod
    def _response_time_penalty(ms: int) -> int:
        """Apply response-time modifier: fast endpoints get full score, slow ones are penalized."""
        if ms < 500:
            return 0
        if ms < 2000:
            return 10
        if ms < 5000:
            return 20
        return 30

    def _check_endpoint(self, endpoint: str) -> EndpointCheck:
        """Check a single endpoint. Interpret status codes with response-time gradient."""
        # SSRF guard: block private/internal IPs
        is_private, dns_failed = self._is_private_endpoint(endpoint)
        if is_private:
            _log.warning("Blocked liveness check to private/internal endpoint: %s", endpoint)
            return EndpointCheck(endpoint=endpoint, status="blocked_private", http_code=0, score=0)
        if dns_failed:
            return EndpointCheck(endpoint=endpoint, status="unreachable", http_code=0, score=0)

        start = time.monotonic()
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
            elapsed = int((time.monotonic() - start) * 1000)
            return EndpointCheck(endpoint=endpoint, status="timeout", http_code=0, score=0, response_time_ms=elapsed)
        except requests.ConnectionError:
            elapsed = int((time.monotonic() - start) * 1000)
            return EndpointCheck(endpoint=endpoint, status="unreachable", http_code=0, score=0, response_time_ms=elapsed)
        except requests.RequestException:
            elapsed = int((time.monotonic() - start) * 1000)
            return EndpointCheck(endpoint=endpoint, status="error", http_code=0, score=0, response_time_ms=elapsed)

        elapsed = int((time.monotonic() - start) * 1000)
        penalty = self._response_time_penalty(elapsed)

        # Status code interpretation with response-time gradient
        if status in (200, 201, 204):
            base_score = max(0, 100 - penalty)
            return EndpointCheck(endpoint=endpoint, status="alive", http_code=status, score=base_score, response_time_ms=elapsed)
        if status in (401, 403):
            # Endpoint exists AND is properly secured -- this is GOOD
            base_score = max(0, 100 - penalty)
            return EndpointCheck(endpoint=endpoint, status="secured", http_code=status, score=base_score, response_time_ms=elapsed)
        if status in (301, 302, 307, 308):
            base_score = max(0, 80 - penalty)
            return EndpointCheck(endpoint=endpoint, status="redirect", http_code=status, score=base_score, response_time_ms=elapsed)
        if status == 404:
            return EndpointCheck(endpoint=endpoint, status="not_found", http_code=status, score=0, response_time_ms=elapsed)
        if status in (500, 502, 503, 504):
            return EndpointCheck(endpoint=endpoint, status="server_error", http_code=status, score=20, response_time_ms=elapsed)
        if status == 429:
            # Rate limited -- endpoint exists and is active
            base_score = max(0, 90 - penalty)
            return EndpointCheck(endpoint=endpoint, status="rate_limited", http_code=status, score=base_score, response_time_ms=elapsed)
        # Unknown status
        return EndpointCheck(endpoint=endpoint, status=f"http_{status}", http_code=status, score=50, response_time_ms=elapsed)
