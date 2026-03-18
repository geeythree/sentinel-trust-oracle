"""Unit tests for AgentVerifier — identity scoring and manifest validation."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import pytest
from unittest.mock import patch, MagicMock
from agent_verifier import AgentVerifier, REQUIRED_FIELDS, OPTIONAL_FIELDS
from models import ManifestValidation


@pytest.fixture
def verifier():
    logger = MagicMock()
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
        # 3 required * 20 = 60, 5 optional * 5 = 25, services with endpoints = 15 → 100
        assert score == 100

    def test_minimal_manifest(self, verifier):
        manifest = {"name": "TestAgent"}
        validation = verifier._validate_manifest(manifest)
        score = verifier._compute_identity_score(manifest, validation)
        # 1 required * 20 = 20, no optional, no valid services → 20
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
            "services": [{"name": "mcp"}],  # no endpoint field
        }
        validation = verifier._validate_manifest(manifest)
        score = verifier._compute_identity_score(manifest, validation)
        # 3 required * 20 = 60, services without endpoints = 0 → 60
        assert score == 60

    def test_mixed_services_no_bonus(self, verifier):
        """Non-dict items in services array should block the 15-point bonus."""
        manifest = {
            "name": "Test",
            "description": "Test",
            "services": [{"endpoint": "https://api.test.com"}, "invalid_string"],
        }
        validation = verifier._validate_manifest(manifest)
        score = verifier._compute_identity_score(manifest, validation)
        # 3 required * 20 = 60, mixed services = 0 → 60
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
        # 3 * 20 = 60, min(25, 5*5) = 25 → 85
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


# --- SSRF protection ---

class TestSSRFProtection:
    def test_blocks_localhost(self, verifier):
        assert verifier._is_private_ip("127.0.0.1") is True

    def test_blocks_private_ranges(self, verifier):
        assert verifier._is_private_ip("10.0.0.1") is True
        assert verifier._is_private_ip("192.168.1.1") is True
        assert verifier._is_private_ip("172.16.0.1") is True

    def test_allows_public_ip(self, verifier):
        assert verifier._is_private_ip("8.8.8.8") is False
        assert verifier._is_private_ip("1.1.1.1") is False

    def test_handles_dns_failure(self, verifier):
        # Non-existent domain should return False (let requests handle it)
        result = verifier._is_private_ip("this-domain-does-not-exist-xxxxxxxxx.com")
        assert result is False


# --- verify with empty URI ---

class TestVerify:
    def test_empty_uri_returns_failure(self, verifier):
        result = verifier.verify("")
        assert result.success is False
        assert result.identity_score == 0

    def test_empty_uri_has_error_message(self, verifier):
        result = verifier.verify("")
        assert result.error_message == "Empty agent URI"
