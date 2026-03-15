"""Endpoint liveness checking for agent services."""
from __future__ import annotations

import requests

from config import config
from logger import AgentLogger
from models import EndpointCheck, LivenessResult


class LivenessChecker:
    """Check each declared service endpoint for liveness."""

    def __init__(self, logger: AgentLogger) -> None:
        self._logger = logger

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
        total_score = sum(d.score for d in details)
        avg_score = total_score // len(details) if details else 0

        return LivenessResult(
            success=True,
            endpoints_declared=len(services),
            endpoints_live=live,
            endpoints_secured=secured,
            endpoints_dead=dead,
            liveness_score=avg_score,
            details=details,
        )

    def _check_endpoint(self, endpoint: str) -> EndpointCheck:
        """Check a single endpoint. Interpret status codes correctly."""
        try:
            # Use HEAD request first (lighter), fall back to GET
            resp = requests.head(
                endpoint, timeout=config.LIVENESS_TIMEOUT, allow_redirects=False
            )
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
        if status in (500, 502, 503):
            return EndpointCheck(endpoint=endpoint, status="server_error", http_code=status, score=20)
        if status == 429:
            # Rate limited -- endpoint exists and is active
            return EndpointCheck(endpoint=endpoint, status="rate_limited", http_code=status, score=90)
        # Unknown status
        return EndpointCheck(endpoint=endpoint, status=f"http_{status}", http_code=status, score=50)
