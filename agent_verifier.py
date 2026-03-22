"""Agent identity verification: fetch manifest, validate, score."""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import socket
from typing import Optional
from urllib.parse import urlparse, urlunparse

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config
from exceptions import IdentityVerificationError
from logger import AgentLogger
from models import IdentityVerification, ManifestValidation

_log = logging.getLogger(__name__)

REQUIRED_FIELDS = ["name", "description", "services"]
OPTIONAL_FIELDS = ["image", "x402Support", "active", "registrations", "supportedTrust"]


class AgentVerifier:
    """Fetch agent.json from URI, resolve IPFS, validate structure, score identity completeness."""

    def __init__(self, logger: AgentLogger) -> None:
        self._logger = logger
        self._session = requests.Session()

    def close(self) -> None:
        """Close the HTTP session."""
        self._session.close()

    def verify(self, agent_uri: str) -> IdentityVerification:
        """Fetch and validate agent manifest from URI."""
        if not agent_uri:
            return IdentityVerification(
                success=False,
                error_message="Empty agent URI",
                identity_score=0,
            )

        try:
            # 1. Resolve URI
            url = self._resolve_uri(agent_uri)

            # 2. Fetch manifest
            manifest = self._fetch_manifest(url)

            # 3. Validate structure
            validation = self._validate_manifest(manifest)

            # 4. Score identity completeness
            score = self._compute_identity_score(manifest, validation)

            return IdentityVerification(
                success=True,
                manifest=manifest,
                uri_resolved=url,
                fields_present=validation.fields_present,
                fields_missing=validation.fields_missing,
                services_declared=len(manifest.get("services", [])),
                identity_score=score,
            )
        except Exception as e:
            return IdentityVerification(
                success=False,
                error_message=str(e),
                identity_score=0,
            )

    def _resolve_uri(self, uri: str) -> str:
        """Resolve ipfs://, data:, and https:// URIs."""
        if uri.startswith("ipfs://"):
            cid = uri[7:]
            # Try first IPFS gateway; fallback handled in _fetch_manifest
            return f"{config.IPFS_GATEWAYS[0]}{cid}"
        if uri.startswith("data:"):
            return uri  # Handle inline in _fetch_manifest
        return uri  # Assume https://

    def _fetch_manifest(self, url: str) -> dict:
        """Fetch manifest JSON. Handle data: URIs inline, IPFS with gateway fallback."""
        # Handle data: URIs
        if url.startswith("data:"):
            return self._decode_data_uri(url)

        # Handle IPFS with gateway fallback
        if any(gw in url for gw in config.IPFS_GATEWAYS):
            cid = None
            for gw in config.IPFS_GATEWAYS:
                if url.startswith(gw):
                    cid = url[len(gw):]
                    break

            if cid:
                for gateway in config.IPFS_GATEWAYS:
                    try:
                        return self._http_fetch(f"{gateway}{cid}")
                    except Exception:
                        continue
                raise IdentityVerificationError(f"All IPFS gateways failed for CID: {cid}")

        # Regular HTTPS fetch
        return self._http_fetch(url)

    @staticmethod
    def _resolve_and_check(hostname: str) -> tuple[bool, Optional[str]]:
        """Resolve hostname and check if IP is private (SSRF guard).

        Returns (is_private, resolved_ip). DNS failure → (True, None) (conservative).
        """
        try:
            resolved = socket.gethostbyname(hostname)
            ip = ipaddress.ip_address(resolved)
            is_private = (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
            )
            return (is_private, resolved)
        except Exception:
            _log.warning("DNS resolution failed for %s — treating as private", hostname)
            return (True, None)  # Conservative: DNS failure = block

    @staticmethod
    def _is_private_ip(hostname: str) -> bool:
        """Legacy compatibility wrapper around _resolve_and_check."""
        is_private, _ = AgentVerifier._resolve_and_check(hostname)
        return is_private

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=10),
    )
    def _http_fetch(self, url: str) -> dict:
        """Fetch JSON from URL with retry. Blocks SSRF via resolved IP pinning."""
        parsed = urlparse(url)
        hostname = parsed.hostname or ""

        is_private, resolved_ip = self._resolve_and_check(hostname)
        if is_private:
            raise IdentityVerificationError(
                f"Refused to fetch from private/internal address: {hostname}"
            )

        # SSRF note: IP pinning breaks HTTPS (TLS cert mismatch on CDNs).
        # HTTPS is already safe against DNS rebinding (TLS verifies hostname).
        # For HTTP, pin the resolved IP to prevent TOCTOU rebinding.
        if resolved_ip and parsed.scheme == "http":
            pinned = parsed._replace(netloc=f"{resolved_ip}:{parsed.port}" if parsed.port else resolved_ip)
            pinned_url = urlunparse(pinned)
            headers = {"Host": hostname}
        else:
            pinned_url = url
            headers = {}

        resp = self._session.get(pinned_url, timeout=30, headers=headers, stream=True)
        resp.raise_for_status()

        # Stream with size cap to prevent memory DoS
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=8192):
            total += len(chunk)
            if total > config.MAX_MANIFEST_BYTES:
                resp.close()
                raise IdentityVerificationError(
                    f"Manifest too large: >{config.MAX_MANIFEST_BYTES} bytes"
                )
            chunks.append(chunk)

        content = b"".join(chunks)
        return json.loads(content)

    def _decode_data_uri(self, data_uri: str) -> dict:
        """Decode data:application/json;base64,<data> or URL-encoded data URI."""
        from urllib.parse import unquote
        # Fix #3: cap data URI size before decoding to prevent memory DoS
        if len(data_uri) > config.MAX_MANIFEST_BYTES * 2:  # base64 ≈ 4/3× raw size
            raise IdentityVerificationError(
                f"data URI too large: {len(data_uri)} chars"
            )
        try:
            # data:application/json;base64,<base64data>
            if ";base64," in data_uri:
                encoded = data_uri.split(";base64,", 1)[1]
                decoded = base64.b64decode(encoded).decode("utf-8")
                if len(decoded) > config.MAX_MANIFEST_BYTES:
                    raise IdentityVerificationError(
                        f"Decoded manifest too large: {len(decoded)} bytes"
                    )
                return json.loads(decoded)
            # data:application/json,<json> (may be URL-encoded)
            elif "," in data_uri:
                json_str = data_uri.split(",", 1)[1]
                # URL-decode percent-encoded characters (%7B -> {, etc.)
                json_str = unquote(json_str)
                return json.loads(json_str)
            else:
                raise IdentityVerificationError(f"Unsupported data URI format")
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
            raise IdentityVerificationError(f"Failed to decode data URI: {e}") from e

    def _validate_manifest(self, manifest: dict) -> ManifestValidation:
        """Check required and optional fields."""
        present = [f for f in REQUIRED_FIELDS if f in manifest]
        missing = [f for f in REQUIRED_FIELDS if f not in manifest]
        optional_present = [f for f in OPTIONAL_FIELDS if f in manifest]

        # Validate services array structure
        services = manifest.get("services", [])
        services_valid = (
            isinstance(services, list)
            and len(services) > 0
            and all(
                isinstance(s, dict) and "endpoint" in s
                for s in services
            )
        )

        return ManifestValidation(
            fields_present=present + optional_present,
            fields_missing=missing,
            services_valid=services_valid,
        )

    def _compute_identity_score(self, manifest: dict, validation: ManifestValidation) -> int:
        """Score 0-100 based on manifest completeness."""
        score = 0

        # Required fields: 20 points each (3 fields = 60 max)
        score += len([f for f in REQUIRED_FIELDS if f in manifest]) * 20

        # Optional fields with bonus: 5 points each (max 25)
        score += min(25, len([f for f in OPTIONAL_FIELDS if f in manifest]) * 5)

        # Services have endpoints: 15 points
        # Fix #6: require ALL items to be dicts with endpoints — no free pass for non-dicts
        services = manifest.get("services", [])
        if (
            services
            and isinstance(services, list)
            and all(isinstance(s, dict) and "endpoint" in s for s in services)
        ):
            score += 15

        return min(100, score)
