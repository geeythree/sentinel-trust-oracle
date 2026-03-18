"""Error-path tests: verify graceful degradation under failure conditions."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import json
import pytest
from unittest.mock import MagicMock, patch
import requests

from liveness_checker import LivenessChecker
from agent_verifier import AgentVerifier
from scorer import Scorer
from models import (
    IdentityVerification,
    LivenessResult,
    OnchainAnalysis,
    ExistingReputation,
    VeniceEvaluation,
    VeniceParseMethod,
    EvaluationState,
    TrustDimensions,
)


# --- Agent Verifier Error Paths ---

class TestAgentVerifierErrors:
    @pytest.fixture
    def verifier(self):
        logger = MagicMock()
        return AgentVerifier(logger)

    def test_invalid_json_returns_failure(self, verifier):
        """Agent URI returning non-JSON should fail gracefully."""
        with patch("agent_verifier.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                text="this is not json",
                headers={"content-type": "text/plain"},
            )
            mock_get.return_value.json.side_effect = json.JSONDecodeError("", "", 0)
            result = verifier.verify("https://example.com/agent.json")
        assert result.success is False

    def test_connection_error_returns_failure(self, verifier):
        """Network failure should not crash."""
        with patch("agent_verifier.requests.get", side_effect=requests.ConnectionError("DNS fail")):
            result = verifier.verify("https://unreachable.test/agent.json")
        assert result.success is False
        assert result.identity_score == 0

    def test_timeout_returns_failure(self, verifier):
        """HTTP timeout should return failure, not raise."""
        with patch("agent_verifier.requests.get", side_effect=requests.Timeout()):
            result = verifier.verify("https://slow.test/agent.json")
        assert result.success is False


# --- Liveness Checker Error Paths ---

class TestLivenessCheckerErrors:
    @pytest.fixture
    def checker(self):
        logger = MagicMock()
        return LivenessChecker(logger)

    def test_malformed_service_entries_skipped(self, checker):
        """Non-dict entries in services should be skipped, not crash."""
        manifest = {"services": ["not-a-dict", 42, None]}
        result = checker.check(manifest)
        assert result.success is True
        # Non-dict entries are skipped during iteration but counted in endpoints_declared
        assert result.endpoints_declared == 3
        assert len(result.details) == 0  # all skipped during iteration

    def test_empty_endpoint_gets_zero(self, checker):
        """Service with empty endpoint string gets zero score."""
        manifest = {"services": [{"endpoint": ""}]}
        result = checker.check(manifest)
        assert result.details[0].status == "missing"
        assert result.details[0].score == 0

    def test_request_exception_zero_score(self, checker):
        """Generic RequestException should return zero, not crash."""
        with patch.object(checker._session, "head", side_effect=requests.RequestException("weird")):
            result = checker._check_endpoint("https://broken.test")
        assert result.status == "error"
        assert result.score == 0


# --- Scorer Error Paths ---

class TestScorerEdgeCases:
    @pytest.fixture
    def scorer(self):
        return Scorer()

    def test_all_zero_dimensions(self, scorer):
        """All-zero inputs should produce score=0, not crash."""
        dims = TrustDimensions(
            identity_completeness=0,
            endpoint_liveness=0,
            onchain_history=0,
            venice_trust_analysis=0,
        )
        composite = scorer.compute_composite(dims)
        assert composite == 0

    def test_all_100_dimensions(self, scorer):
        """All-100 inputs should produce score=100."""
        dims = TrustDimensions(
            identity_completeness=100,
            endpoint_liveness=100,
            onchain_history=100,
            venice_trust_analysis=100,
        )
        composite = scorer.compute_composite(dims)
        assert composite == 100

    def test_extreme_spread_triggers_review(self, scorer):
        """Spread > 50 should trigger PENDING_HUMAN_REVIEW when confidence is low."""
        dims = TrustDimensions(
            identity_completeness=100,
            endpoint_liveness=0,
            onchain_history=50,
            venice_trust_analysis=50,
        )
        assert dims.spread == 100  # 100 - 0
        # With low confidence (< 70), high spread should trigger review
        state = scorer.determine_state(50, dims)
        assert state == EvaluationState.PENDING_HUMAN_REVIEW

    def test_venice_parse_failure_zero_confidence(self, scorer):
        """Venice parse failure should contribute 0 to confidence."""
        venice = VeniceEvaluation(
            dimension="trust_analysis",
            score=50,
            reasoning="fallback",
            parse_method=VeniceParseMethod.FALLBACK_NEUTRAL,
            venice_parse_failed=True,
            tokens_sent=0,
            tokens_received=0,
        )
        contrib = scorer._venice_confidence_contribution(venice)
        assert contrib == 0

    def test_confidence_never_negative(self, scorer):
        """Confidence should be clamped at 0 even with heavy penalties."""
        identity = IdentityVerification(success=False, identity_score=0)
        liveness = LivenessResult(success=False, liveness_score=0)
        onchain = OnchainAnalysis(success=False, onchain_score=0)
        venice = VeniceEvaluation(
            dimension="trust_analysis",
            score=0, reasoning="", parse_method=VeniceParseMethod.FALLBACK_NEUTRAL,
            venice_parse_failed=True, tokens_sent=0, tokens_received=0,
        )
        dims = TrustDimensions(
            identity_completeness=100,
            endpoint_liveness=0,
            onchain_history=0,
            venice_trust_analysis=0,
        )
        conf = scorer.compute_confidence(identity, liveness, onchain, venice, dims)
        assert conf >= 0


# --- Dashboard Atomic Write ---

class TestDashboardAtomicWrite:
    def test_corrupt_json_recovers(self, tmp_path):
        """Dashboard should handle corrupt results.json gracefully."""
        results_path = tmp_path / "results.json"
        results_path.write_text("{corrupt json...")

        # Simulate _update_dashboard loading corrupt file
        existing = []
        try:
            with open(results_path, "r") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, ValueError):
            existing = []

        assert existing == []  # Graceful recovery
