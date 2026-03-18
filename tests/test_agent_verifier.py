"""Unit tests for AgentVerifier — real DNS, real data URIs, real scoring. No mocks."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import base64
import json
import pytest
from logger import AgentLogger
from pathlib import Path
from agent_verifier import AgentVerifier, REQUIRED_FIELDS, OPTIONAL_FIELDS
from models import ManifestValidation


@pytest.fixture
def verifier(tmp_path):
    logger = AgentLogger(tmp_path / "test_log.json", budget=15)
    return AgentVerifier(logger)


# --- _compute_identity_score ---

class TestIdentityScore:
    def test_complete_manifest_max_score(self, verifier):
        manifest = {
            "name": "TestAgent",
            "description": "A test agent",
            "services": [{"endpoint": "https://api.test.com"}],
            "image": "https://img.com/logo.png",
            "x402Support": False,
            "active": True,
            "registrations": [],
            "supportedTrust": [],
        }
        validation = verifier._validate_manifest(manifest)
        score = verifier._compute_identity_score(manifest, validation)
        assert score == 100

    def test_minimal_manifest(self, verifier):
        manifest = {"name": "TestAgent"}
        validation = verifier._validate_manifest(manifest)
        score = verifier._compute_identity_score(manifest, validation)
        assert score == 20

    def test_empty_manifest(self, verifier):
        manifest = {}
        validation = verifier._validate_manifest(manifest)
        score = verifier._compute_identity_score(manifest, validation)
        assert score == 0

    def test_services_without_endpoints_no_bonus(self, verifier):
        manifest = {
            "name": "Test",
            "description": "Test",
            "services": [{"name": "mcp"}],
        }
        validation = verifier._validate_manifest(manifest)
        score = verifier._compute_identity_score(manifest, validation)
        assert score == 60

    def test_mixed_services_no_bonus(self, verifier):
        manifest = {
            "name": "Test",
            "description": "Test",
            "services": [{"endpoint": "https://api.test.com"}, "invalid_string"],
        }
        validation = verifier._validate_manifest(manifest)
        score = verifier._compute_identity_score(manifest, validation)
        assert score == 60

    def test_optional_fields_capped_at_25(self, verifier):
        manifest = {
            "name": "Test",
            "description": "Test",
            "services": [],
            "image": "img",
            "x402Support": True,
            "active": True,
            "registrations": [],
            "supportedTrust": [],
        }
        validation = verifier._validate_manifest(manifest)
        score = verifier._compute_identity_score(manifest, validation)
        assert score == 85


# --- _validate_manifest ---

class TestValidateManifest:
    def test_all_required_present(self, verifier):
        manifest = {"name": "A", "description": "B", "services": [{"endpoint": "http://x"}]}
        v = verifier._validate_manifest(manifest)
        assert v.fields_missing == []
        assert v.services_valid is True

    def test_missing_required_fields(self, verifier):
        manifest = {"name": "A"}
        v = verifier._validate_manifest(manifest)
        assert "description" in v.fields_missing
        assert "services" in v.fields_missing

    def test_empty_services_not_valid(self, verifier):
        manifest = {"name": "A", "description": "B", "services": []}
        v = verifier._validate_manifest(manifest)
        assert v.services_valid is False

    def test_services_missing_endpoint_not_valid(self, verifier):
        manifest = {"name": "A", "description": "B", "services": [{"name": "test"}]}
        v = verifier._validate_manifest(manifest)
        assert v.services_valid is False


# --- SSRF protection with real DNS ---

class TestSSRFProtection:
    def test_blocks_localhost_ip(self, verifier):
        """127.0.0.1 is a private IP — must be blocked."""
        is_private, resolved = verifier._resolve_and_check("127.0.0.1")
        assert is_private is True
        assert resolved == "127.0.0.1"

    def test_blocks_localhost_hostname(self, verifier):
        """localhost resolves to 127.0.0.1 — must be blocked."""
        is_private, resolved = verifier._resolve_and_check("localhost")
        assert is_private is True
        assert resolved == "127.0.0.1"

    def test_blocks_private_ranges(self, verifier):
        assert verifier._resolve_and_check("10.0.0.1")[0] is True
        assert verifier._resolve_and_check("192.168.1.1")[0] is True
        assert verifier._resolve_and_check("172.16.0.1")[0] is True

    def test_allows_public_ip(self, verifier):
        is_private, resolved = verifier._resolve_and_check("8.8.8.8")
        assert is_private is False
        assert resolved == "8.8.8.8"

    def test_dns_failure_treated_as_private(self, verifier):
        """Non-existent domain should return (True, None) — conservative block."""
        is_private, resolved = verifier._resolve_and_check("this-domain-does-not-exist-xxxxxxxxx.com")
        assert is_private is True
        assert resolved is None

    def test_legacy_is_private_ip_still_works(self, verifier):
        """_is_private_ip backward compat wrapper."""
        assert verifier._is_private_ip("127.0.0.1") is True
        assert verifier._is_private_ip("8.8.8.8") is False


# --- Real data URI decoding ---

class TestDataURIs:
    def test_base64_data_uri(self, verifier):
        manifest = {"name": "Test", "description": "B", "services": []}
        encoded = base64.b64encode(json.dumps(manifest).encode()).decode()
        data_uri = f"data:application/json;base64,{encoded}"
        result = verifier._decode_data_uri(data_uri)
        assert result["name"] == "Test"

    def test_url_encoded_data_uri(self, verifier):
        manifest_json = '{"name":"Test","description":"B"}'
        from urllib.parse import quote
        encoded = quote(manifest_json)
        data_uri = f"data:application/json,{encoded}"
        result = verifier._decode_data_uri(data_uri)
        assert result["name"] == "Test"

    def test_oversized_data_uri_rejected(self, verifier):
        huge = "A" * (cm.config.MAX_MANIFEST_BYTES * 2 + 1)
        data_uri = f"data:application/json;base64,{huge}"
        from exceptions import IdentityVerificationError
        with pytest.raises(IdentityVerificationError, match="too large"):
            verifier._decode_data_uri(data_uri)


# --- verify with empty URI ---

class TestVerify:
    def test_empty_uri_returns_failure(self, verifier):
        result = verifier.verify("")
        assert result.success is False
        assert result.identity_score == 0

    def test_empty_uri_has_error_message(self, verifier):
        result = verifier.verify("")
        assert result.error_message == "Empty agent URI"


# --- Session lifecycle ---

class TestSessionLifecycle:
    def test_verifier_has_session(self, verifier):
        assert hasattr(verifier, '_session')
        assert verifier._session is not None

    def test_close_does_not_error(self, verifier):
        verifier.close()  # Should not raise
