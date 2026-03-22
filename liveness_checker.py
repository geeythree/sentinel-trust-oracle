"""Endpoint liveness checking for agent services."""
from __future__ import annotations

import ipaddress
import json
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
            # Still score ERC-8004 protocol compliance even with no services
            protocol_score = self._check_protocol_compliance(manifest, [])
            return LivenessResult(
                success=True,
                endpoints_declared=0,
                endpoints_live=0,
                endpoints_secured=0,
                endpoints_dead=0,
                liveness_score=0,
                protocol_compliance_score=protocol_score,
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

        # MCP + ERC-8004 protocol compliance check
        protocol_score = self._check_protocol_compliance(manifest, services)

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

    def _try_mcp_handshake(self, endpoint: str) -> tuple[bool, str]:
        """Attempt MCP initialize handshake per spec 2025-03-26.

        POSTs JSON-RPC initialize with Accept: application/json, text/event-stream.
        Handles both plain JSON responses (Streamable HTTP transport) and
        SSE event streams (legacy SSE transport).

        Returns (success, protocol_version_str).
        """
        is_private, dns_failed = self._is_private_endpoint(endpoint)
        if is_private or dns_failed:
            return (False, "")

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "sentinel-trust-oracle", "version": "1.0"},
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        try:
            resp = self._session.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=5,
                allow_redirects=False,
                stream=True,
            )
            if resp.status_code not in (200, 201):
                return (False, "")

            content_type = resp.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                # SSE transport: read first data event
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if raw_line and raw_line.startswith("data:"):
                        data_str = raw_line[5:].strip()
                        try:
                            data = json.loads(data_str)
                            version = data.get("result", {}).get("protocolVersion", "")
                            return (bool(version), version)
                        except Exception:
                            pass
                        break
            else:
                # Streamable HTTP (or plain JSON) transport
                try:
                    data = resp.json()
                    version = data.get("result", {}).get("protocolVersion", "")
                    return (bool(version), version)
                except Exception:
                    pass

            return (False, "")
        except Exception:
            return (False, "")

    def _check_protocol_compliance(self, manifest: dict, services: list[dict]) -> int:
        """ERC-8004 + MCP protocol compliance scoring.

        Scores two layers of compliance:

        Layer 1 — ERC-8004 manifest compliance (the protocol being evaluated):
          - Has ERC-8004 type URI in manifest        → +10
          - Services declare skills (interface spec)  → +20
          - Services declare domains                  → +15
          - Manifest has task_categories              → +10
          - Has live HTTP infrastructure              → +35 baseline

        Layer 2 — MCP transport signals (bonus on top):
          - Declares stdio/SSE/streamable-http        → +15
          - Successful MCP initialize handshake       → 100 (overrides all)

        Resulting bands:
          100  : MCP handshake confirmed
          80-95: Full ERC-8004 compliance + MCP transport declared
          65-80: Full ERC-8004 compliance (skills + domains + type + task_categories)
          45-65: Partial ERC-8004 compliance
          35   : HTTP infrastructure only, no metadata
          20   : stdio-only agent (no HTTP to verify)
          0    : No protocol signals
        """
        has_mcp_transport = False
        has_skills = False
        has_domains = False
        http_endpoints: list[str] = []

        for svc in services:
            if not isinstance(svc, dict):
                continue
            endpoint = svc.get("endpoint", "")
            transport = svc.get("transport", "").lower()
            protocol = svc.get("protocol", "").lower()

            if transport in ("stdio", "sse", "streamable-http"):
                has_mcp_transport = True
            if "mcp" in protocol or "mcp" in svc.get("type", "").lower():
                has_mcp_transport = True
            if isinstance(endpoint, str) and "/sse" in endpoint.lower():
                has_mcp_transport = True
            if svc.get("skills"):
                has_skills = True
            if svc.get("domains"):
                has_domains = True
            if isinstance(endpoint, str) and endpoint.startswith(("http://", "https://")):
                http_endpoints.append(endpoint)

        # Attempt real MCP handshake (best possible signal)
        handshake_success = False
        handshake_version = ""
        for ep in http_endpoints:
            ok, version = self._try_mcp_handshake(ep)
            if ok:
                handshake_success = True
                handshake_version = version
                _log.info("MCP handshake success: endpoint=%s protocolVersion=%s", ep, version)
                break
            else:
                _log.debug("MCP handshake failed for endpoint: %s", ep)

        if handshake_success:
            score = 100
        else:
            # ERC-8004 manifest compliance scoring
            has_erc8004_type = bool(manifest.get("type", ""))
            has_task_categories = bool(manifest.get("task_categories"))

            score = 0
            if http_endpoints:
                score = 35  # baseline: has live HTTP infrastructure

            if has_erc8004_type:
                score += 10  # declares ERC-8004 type URI — protocol-aware agent
            if has_skills:
                score += 20  # service-level skill declarations — core ERC-8004 interface
            if has_domains:
                score += 15  # domain declarations — agent capability taxonomy
            if has_task_categories:
                score += 10  # top-level task taxonomy

            # MCP transport bonus
            if has_mcp_transport and http_endpoints:
                score += 15  # declared MCP transport + HTTP infrastructure
            elif has_mcp_transport:
                score = max(score, 20)  # stdio-only: no HTTP to probe but transport declared

            score = min(100, score)

        _log.info(
            "Protocol compliance: score=%d handshake=%s version=%r mcp_transport=%s "
            "skills=%s domains=%s erc8004_type=%s http_eps=%d",
            score, handshake_success, handshake_version,
            has_mcp_transport, has_skills, has_domains,
            bool(manifest.get("type")), len(http_endpoints),
        )
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
