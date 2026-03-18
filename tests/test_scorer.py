"""Unit tests for Scorer — composite scoring, confidence, and state determination."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set up config before imports
os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import pytest
from scorer import Scorer
from models import (
    EvaluationState,
    IdentityVerification,
    LivenessResult,
    OnchainAnalysis,
    ExistingReputation,
    TrustDimensions,
    VeniceEvaluation,
    VeniceParseMethod,
)


@pytest.fixture
def scorer():
    return Scorer()


# --- compute_composite ---

class TestComputeComposite:
    def test_all_100(self, scorer):
        dims = TrustDimensions(100, 100, 100, 100)
        assert scorer.compute_composite(dims) == 100

    def test_all_0(self, scorer):
        dims = TrustDimensions(0, 0, 0, 0)
        assert scorer.compute_composite(dims) == 0

    def test_all_50(self, scorer):
        dims = TrustDimensions(50, 50, 50, 50)
        assert scorer.compute_composite(dims) == 50

    def test_weights_applied(self, scorer):
        # Identity=100(20%), Liveness=0, Onchain=0, Venice=0
        dims = TrustDimensions(100, 0, 0, 0)
        assert scorer.compute_composite(dims) == 20

    def test_venice_weighted_highest(self, scorer):
        # Venice=100(30%) should contribute most
        dims = TrustDimensions(0, 0, 0, 100)
        assert scorer.compute_composite(dims) == 30

    def test_clamped_to_100(self, scorer):
        # Even with impossible inputs, should never exceed 100
        dims = TrustDimensions(100, 100, 100, 100)
        assert scorer.compute_composite(dims) <= 100

    def test_realistic_score(self, scorer):
        # Agent with good identity, partial liveness, new wallet, decent Venice
        dims = TrustDimensions(85, 75, 50, 70)
        # 85*0.20 + 75*0.25 + 50*0.25 + 70*0.30 = 17 + 18.75 + 12.5 + 21 = 69.25 → 69
        assert scorer.compute_composite(dims) == 69


# --- compute_confidence ---

class TestComputeConfidence:
    def _make_identity(self, success=True, missing=None):
        return IdentityVerification(
            success=success,
            fields_missing=missing or [],
            identity_score=80,
        )

    def _make_liveness(self, success=True, declared=3, dead=0):
        return LivenessResult(
            success=success,
            endpoints_declared=declared,
            endpoints_dead=dead,
        )

    def _make_onchain(self, success=True, tx_count=10):
        return OnchainAnalysis(
            success=success,
            transaction_count=tx_count,
            onchain_score=60,
        )

    def _make_venice(self, parse_method=VeniceParseMethod.JSON_SCHEMA, failed=False):
        return VeniceEvaluation(
            dimension="trust_analysis",
            score=70,
            reasoning="test",
            parse_method=parse_method,
            venice_parse_failed=failed,
        )

    def test_perfect_confidence(self, scorer):
        """Complete identity + all endpoints live + JSON schema + has tx history → 100."""
        dims = TrustDimensions(80, 80, 80, 80)
        conf = scorer.compute_confidence(
            self._make_identity(),
            self._make_liveness(),
            self._make_onchain(),
            self._make_venice(),
            dims,
        )
        assert conf == 100

    def test_partial_identity_reduces_confidence(self, scorer):
        dims = TrustDimensions(80, 80, 80, 80)
        conf = scorer.compute_confidence(
            self._make_identity(missing=["description"]),
            self._make_liveness(),
            self._make_onchain(),
            self._make_venice(),
            dims,
        )
        # 15 (partial) + 25 + 25 + 25 = 90
        assert conf == 90

    def test_failed_venice_reduces_confidence(self, scorer):
        dims = TrustDimensions(80, 80, 80, 50)
        conf = scorer.compute_confidence(
            self._make_identity(),
            self._make_liveness(),
            self._make_onchain(),
            self._make_venice(failed=True),
            dims,
        )
        # 25 + 25 + 25 + 0 = 75
        assert conf == 75

    def test_high_spread_penalty(self, scorer):
        """Spread > 50 → -30 penalty."""
        dims = TrustDimensions(100, 100, 40, 100)  # spread = 60
        conf = scorer.compute_confidence(
            self._make_identity(),
            self._make_liveness(),
            self._make_onchain(),
            self._make_venice(),
            dims,
        )
        # 25 + 25 + 25 + 25 - 30 = 70
        assert conf == 70

    def test_new_wallet_gets_partial_credit(self, scorer):
        dims = TrustDimensions(80, 80, 50, 80)
        conf = scorer.compute_confidence(
            self._make_identity(),
            self._make_liveness(),
            self._make_onchain(tx_count=0),
            self._make_venice(),
            dims,
        )
        # 25 + 25 + 15 + 25 = 90
        assert conf == 90

    def test_clamped_to_0(self, scorer):
        dims = TrustDimensions(100, 0, 0, 0)  # spread = 100
        conf = scorer.compute_confidence(
            self._make_identity(success=False),
            self._make_liveness(success=False),
            self._make_onchain(success=False, tx_count=0),
            self._make_venice(failed=True),
            dims,
        )
        assert conf >= 0


# --- determine_state ---

class TestDetermineState:
    def test_high_confidence_verified(self, scorer):
        dims = TrustDimensions(80, 80, 80, 80)
        assert scorer.determine_state(85, dims) == EvaluationState.VERIFIED

    def test_threshold_exactly_70_verified(self, scorer):
        dims = TrustDimensions(80, 80, 80, 80)
        assert scorer.determine_state(70, dims) == EvaluationState.VERIFIED

    def test_low_confidence_low_spread_withheld(self, scorer):
        dims = TrustDimensions(50, 50, 50, 50)  # spread = 0
        assert scorer.determine_state(60, dims) == EvaluationState.WITHHELD_LOW_CONFIDENCE

    def test_low_confidence_high_spread_human_review(self, scorer):
        dims = TrustDimensions(100, 100, 40, 100)  # spread = 60
        assert scorer.determine_state(60, dims) == EvaluationState.PENDING_HUMAN_REVIEW


# --- TrustDimensions ---

class TestTrustDimensions:
    def test_spread_identical_scores(self):
        dims = TrustDimensions(50, 50, 50, 50)
        assert dims.spread == 0

    def test_spread_varied_scores(self):
        dims = TrustDimensions(100, 0, 50, 75)
        assert dims.spread == 100

    def test_as_list(self):
        dims = TrustDimensions(10, 20, 30, 40)
        assert dims.as_list == [10, 20, 30, 40]
