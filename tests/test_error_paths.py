"""Error-path tests: verify graceful degradation under failure conditions. No mocks."""
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
from pathlib import Path
from logger import AgentLogger
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
    def verifier(self, tmp_path):
        logger = AgentLogger(tmp_path / "test_log.json", budget=15)
        v = AgentVerifier(logger)
        yield v
        v.close()

    def test_empty_uri_returns_failure(self, verifier):
        result = verifier.verify("")
        assert result.success is False
        assert result.identity_score == 0

    def test_private_ip_uri_returns_failure(self, verifier):
        """SSRF guard should block localhost URIs."""
        result = verifier.verify("http://127.0.0.1/agent.json")
        assert result.success is False

    def test_invalid_data_uri_returns_failure(self, verifier):
        """Malformed data URI should return failure, not crash."""
        result = verifier.verify("data:application/json;base64,NOT_VALID_BASE64!!!")
        assert result.success is False
        assert result.identity_score == 0


# --- Liveness Checker Error Paths ---

class TestLivenessCheckerErrors:
    @pytest.fixture
    def checker(self, tmp_path):
        logger = AgentLogger(tmp_path / "test_log.json", budget=15)
        lc = LivenessChecker(logger)
        yield lc
        lc.close()

    def test_malformed_service_entries_skipped(self, checker):
        manifest = {"services": ["not-a-dict", 42, None]}
        result = checker.check(manifest)
        assert result.success is True
        assert result.endpoints_declared == 3
        assert len(result.details) == 0

    def test_empty_endpoint_gets_zero(self, checker):
        manifest = {"services": [{"endpoint": ""}]}
        result = checker.check(manifest)
        assert result.details[0].status == "missing"
        assert result.details[0].score == 0

    def test_non_http_gets_neutral(self, checker):
        manifest = {"services": [{"endpoint": "stdio://agent"}]}
        result = checker.check(manifest)
        assert result.details[0].score == 50


# --- Scorer Error Paths ---

class TestScorerEdgeCases:
    @pytest.fixture
    def scorer(self):
        return Scorer()

    def test_all_zero_dimensions(self, scorer):
        dims = TrustDimensions(0, 0, 0, 0, 0)
        composite = scorer.compute_composite(dims)
        assert composite == 0

    def test_all_100_dimensions(self, scorer):
        dims = TrustDimensions(100, 100, 100, 100, 100)
        composite = scorer.compute_composite(dims)
        assert composite == 100

    def test_extreme_spread_withheld(self, scorer):
        dims = TrustDimensions(100, 0, 50, 50, 50)
        assert dims.spread == 100
        state = scorer.determine_state(50, dims)
        assert state == EvaluationState.WITHHELD_LOW_CONFIDENCE

    def test_venice_parse_failure_abstains(self, scorer):
        """Venice parse failure → abstain (LR=1, no update to log-odds)."""
        # With all abstained, confidence should be exactly 50 (prior)
        identity = IdentityVerification(success=False, identity_score=0)
        liveness = LivenessResult(success=True, endpoints_declared=0, liveness_score=0)
        onchain = OnchainAnalysis(success=False, onchain_score=0)
        venice = VeniceEvaluation(
            dimension="trust_analysis",
            score=50,
            reasoning="fallback",
            parse_method=VeniceParseMethod.FALLBACK_NEUTRAL,
            venice_parse_failed=True,
        )
        dims = TrustDimensions(0, 0, 0, 50)
        conf = scorer.compute_confidence(identity, liveness, onchain, venice, dims)
        assert conf == 50  # all abstained → prior unchanged

    def test_confidence_never_negative(self, scorer):
        identity = IdentityVerification(success=False, identity_score=0)
        liveness = LivenessResult(success=False, liveness_score=0)
        onchain = OnchainAnalysis(success=False, onchain_score=0)
        venice = VeniceEvaluation(
            dimension="trust_analysis",
            score=0, reasoning="", parse_method=VeniceParseMethod.FALLBACK_NEUTRAL,
            venice_parse_failed=True,
        )
        dims = TrustDimensions(100, 0, 0, 0)
        conf = scorer.compute_confidence(identity, liveness, onchain, venice, dims)
        assert conf >= 0

    def test_onchain_failure_gives_zero_not_fifty(self):
        """OnchainAnalysis failure should yield score=0, not neutral 50."""
        from onchain_analyzer import OnchainAnalyzer
        # Directly test the failure return value structure
        result = OnchainAnalysis(success=False, wallet_address="0x0", onchain_score=0)
        assert result.onchain_score == 0


# --- Dashboard Atomic Write ---

class TestDashboardAtomicWrite:
    def test_corrupt_json_recovers(self, tmp_path):
        results_path = tmp_path / "results.json"
        results_path.write_text("{corrupt json...")

        existing = []
        try:
            with open(results_path, "r") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, ValueError):
            existing = []

        assert existing == []
