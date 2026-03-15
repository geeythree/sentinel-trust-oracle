"""Agent identity verification: fetch manifest, validate, score."""
from __future__ import annotations

import base64
import json
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config
from exceptions import IdentityVerificationError
from logger import AgentLogger
from models import IdentityVerification, ManifestValidation

REQUIRED_FIELDS = ["name", "description", "services"]
OPTIONAL_FIELDS = ["image", "x402Support", "active", "registrations", "supportedTrust"]


class AgentVerifier:
    """Fetch agent.json from URI, resolve IPFS, validate structure, score identity completeness."""

    def __init__(self, logger: AgentLogger) -> None:
        self._logger = logger

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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=10),
    )
    def _http_fetch(self, url: str) -> dict:
        """Fetch JSON from URL with retry."""
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _decode_data_uri(self, data_uri: str) -> dict:
        """Decode data:application/json;base64,<data> URI."""
        try:
            # data:application/json;base64,<base64data>
            if ";base64," in data_uri:
                encoded = data_uri.split(";base64,", 1)[1]
                decoded = base64.b64decode(encoded).decode("utf-8")
                return json.loads(decoded)
            # data:application/json,<json>
            elif "," in data_uri:
                json_str = data_uri.split(",", 1)[1]
                return json.loads(json_str)
            else:
                raise IdentityVerificationError(f"Unsupported data URI format")
        except (json.JSONDecodeError, Exception) as e:
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
        services = manifest.get("services", [])
        if services and all("endpoint" in s for s in services if isinstance(s, dict)):
            score += 15

        return min(100, score)
