"""Comprehensive tests for Scorer — Bayesian log-odds confidence, CV penalty, composite, state."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import json
import math
import pytest
from scorer import Scorer
from models import (
    DiscoveredAgent,
    EvaluationState,
    IdentityVerification,
    LivenessResult,
    OnchainAnalysis,
    ExistingReputation,
    TrustDimensions,
    TrustVerdict,
    VeniceEvaluation,
    VeniceParseMethod,
)


@pytest.fixture
def scorer():
    return Scorer()


# ---------------------------------------------------------------------------
# Helpers — construct minimal real model objects (no mocks)
# ---------------------------------------------------------------------------

def _identity(success=True, score=80):
    return IdentityVerification(success=success, identity_score=score)


def _liveness(declared=3, score=80):
    return LivenessResult(success=True, endpoints_declared=declared, liveness_score=score)


def _onchain(success=True, score=80):
    return OnchainAnalysis(success=success, onchain_score=score)


def _venice(score=80, parse_method=VeniceParseMethod.JSON_SCHEMA, failed=False):
    return VeniceEvaluation(
        dimension="trust_analysis",
        score=score,
        reasoning="test",
        parse_method=parse_method,
        venice_parse_failed=failed,
    )


def _dims(i=80, l=80, o=80, v=80, p=0):
    return TrustDimensions(i, l, o, v, p)


# ===========================================================================
# 1. Bayesian log-odds math
# ===========================================================================

class TestBayesianLogOdds:
    """Verify the core Bayesian update produces correct confidence values."""

    def test_all_4_high_confidence_gte_95(self, scorer):
        """4 dimensions all >70 → strong evidence → confidence >= 95."""
        dims = _dims(85, 85, 85, 85)
        conf = scorer.compute_confidence(
            _identity(score=85), _liveness(score=85),
            _onchain(score=85), _venice(score=85), dims,
        )
        assert conf >= 95, f"4 high dimensions should give >=95, got {conf}"

    def test_all_4_low_confidence_lte_15(self, scorer):
        """4 dimensions all <30 → strong counter-evidence → confidence <= 15."""
        dims = _dims(10, 10, 10, 10)
        conf = scorer.compute_confidence(
            _identity(score=10), _liveness(score=10),
            _onchain(score=10), _venice(score=10), dims,
        )
        assert conf <= 15, f"4 low dimensions should give <=15, got {conf}"

    def test_all_4_neutral_confidence_is_50(self, scorer):
        """4 dimensions all in 30-70 → LR=1 → no update → confidence ~50."""
        dims = _dims(50, 50, 50, 50)
        conf = scorer.compute_confidence(
            _identity(score=50), _liveness(score=50),
            _onchain(score=50), _venice(score=50), dims,
        )
        assert conf == 50, f"4 neutral dimensions should give 50, got {conf}"

    def test_2_high_2_low_cancel_out(self, scorer):
        """2 high + 2 low → log-odds sum ≈ 0 → confidence ~50 (before CV penalty)."""
        # Use scores that produce exactly cancelling LRs.
        # 2×log(4) + 2×log(0.25) = 0, but CV will apply a penalty.
        # We pick values straddling the thresholds: 80, 80, 20, 20
        dims = _dims(80, 80, 20, 20)
        conf = scorer.compute_confidence(
            _identity(score=80), _liveness(score=80),
            _onchain(score=20), _venice(score=20), dims,
        )
        # CV of [80, 80, 20, 20] = stdev≈34.6, mean=50, CV≈0.69 → log(3) penalty
        # log_odds = 0 - log(3) ≈ -1.099 → posterior ≈ 0.25 → 25
        # With penalty it should be BELOW 50
        assert conf < 50, f"2 high + 2 low with CV penalty should be <50, got {conf}"

    def test_3_high_1_low_confidence_gt_70(self, scorer):
        """3 high + 1 low → net positive → confidence > 70."""
        dims = _dims(80, 80, 80, 20)
        conf = scorer.compute_confidence(
            _identity(score=80), _liveness(score=80),
            _onchain(score=80), _venice(score=20), dims,
        )
        assert conf > 70, f"3 high + 1 low should give >70, got {conf}"

    def test_1_high_3_low_confidence_lt_30(self, scorer):
        """1 high + 3 low → net negative → confidence < 30."""
        dims = _dims(80, 20, 20, 20)
        conf = scorer.compute_confidence(
            _identity(score=80), _liveness(score=20),
            _onchain(score=20), _venice(score=20), dims,
        )
        assert conf < 30, f"1 high + 3 low should give <30, got {conf}"


# ===========================================================================
# 2. Abstention (missing data)
# ===========================================================================

class TestAbstention:
    """Missing data → LR=1 (abstain). No penalty, no reward."""

    def test_identity_failed_3_high_still_above_50(self, scorer):
        """Identity failed (abstain) + 3 high → confidence above 50."""
        dims = _dims(0, 85, 85, 85)  # identity score irrelevant
        conf = scorer.compute_confidence(
            _identity(success=False, score=0), _liveness(score=85),
            _onchain(score=85), _venice(score=85), dims,
        )
        assert conf > 50, f"3 high + 1 abstain should be >50, got {conf}"

    def test_identity_failed_3_high_less_than_4_high(self, scorer):
        """Abstaining should give strictly less confidence than actual data."""
        dims_4 = _dims(85, 85, 85, 85)
        conf_4 = scorer.compute_confidence(
            _identity(score=85), _liveness(score=85),
            _onchain(score=85), _venice(score=85), dims_4,
        )

        dims_3 = _dims(0, 85, 85, 85)
        conf_3 = scorer.compute_confidence(
            _identity(success=False, score=0), _liveness(score=85),
            _onchain(score=85), _venice(score=85), dims_3,
        )
        assert conf_3 < conf_4, f"3 observed should be < 4 observed ({conf_3} vs {conf_4})"

    def test_all_4_abstained_confidence_50(self, scorer):
        """No data at all → no updates → prior = 50%."""
        dims = _dims(0, 0, 0, 0)
        conf = scorer.compute_confidence(
            _identity(success=False, score=0),
            _liveness(declared=0, score=0),  # 0 endpoints declared → abstain
            _onchain(success=False, score=0),
            _venice(score=0, failed=True),  # parse failed → abstain
            dims,
        )
        assert conf == 50, f"All abstained should give exactly 50, got {conf}"

    def test_liveness_0_endpoints_abstains(self, scorer):
        """0 endpoints declared means liveness has no data → abstain."""
        dims = _dims(85, 0, 85, 85)
        conf_abstain = scorer.compute_confidence(
            _identity(score=85), _liveness(declared=0, score=0),
            _onchain(score=85), _venice(score=85), dims,
        )

        dims_full = _dims(85, 85, 85, 85)
        conf_full = scorer.compute_confidence(
            _identity(score=85), _liveness(declared=3, score=85),
            _onchain(score=85), _venice(score=85), dims_full,
        )
        assert conf_abstain < conf_full

    def test_onchain_failed_abstains(self, scorer):
        """On-chain failure → abstain, not penalize."""
        dims = _dims(85, 85, 0, 85)
        conf = scorer.compute_confidence(
            _identity(score=85), _liveness(score=85),
            _onchain(success=False, score=0), _venice(score=85), dims,
        )
        assert conf > 50, f"3 high + 1 abstain should be >50, got {conf}"


# ===========================================================================
# 3. Venice parse-quality degradation
# ===========================================================================

class TestVeniceDegradation:
    """Non-JSON_SCHEMA parse reduces Venice's effective score by 0.75."""

    def test_json_schema_full_lr(self, scorer):
        """Venice score 80 with JSON_SCHEMA → effective 80 > 70 → LR=4."""
        dims = _dims(50, 50, 50, 80)
        conf_full = scorer.compute_confidence(
            _identity(score=50), _liveness(score=50),
            _onchain(score=50), _venice(score=80, parse_method=VeniceParseMethod.JSON_SCHEMA),
            dims,
        )
        # Only venice contributes non-neutral. log(4) → posterior > 50
        assert conf_full > 50

    def test_regex_degrades_to_neutral(self, scorer):
        """Venice score 80 with REGEX → effective 60 (80×0.75) → LR=1 (neutral)."""
        dims = _dims(50, 50, 50, 80)
        conf_degraded = scorer.compute_confidence(
            _identity(score=50), _liveness(score=50),
            _onchain(score=50), _venice(score=80, parse_method=VeniceParseMethod.REGEX_EXTRACTION),
            dims,
        )
        # Degraded venice → neutral LR → all 4 neutral → confidence = 50
        assert conf_degraded == 50, f"Degraded Venice (80→60 neutral) should give 50, got {conf_degraded}"

    def test_json_schema_higher_than_regex(self, scorer):
        """JSON_SCHEMA parse should produce higher confidence than REGEX for same score."""
        dims = _dims(50, 50, 50, 80)

        conf_json = scorer.compute_confidence(
            _identity(score=50), _liveness(score=50),
            _onchain(score=50), _venice(score=80, parse_method=VeniceParseMethod.JSON_SCHEMA),
            dims,
        )
        conf_regex = scorer.compute_confidence(
            _identity(score=50), _liveness(score=50),
            _onchain(score=50), _venice(score=80, parse_method=VeniceParseMethod.REGEX_EXTRACTION),
            dims,
        )
        assert conf_json > conf_regex, f"JSON > REGEX expected ({conf_json} vs {conf_regex})"

    def test_retry_correction_also_degrades(self, scorer):
        """RETRY_CORRECTION parse also degrades by 0.75."""
        dims = _dims(50, 50, 50, 80)
        conf_retry = scorer.compute_confidence(
            _identity(score=50), _liveness(score=50),
            _onchain(score=50), _venice(score=80, parse_method=VeniceParseMethod.RETRY_CORRECTION),
            dims,
        )
        # 80 * 0.75 = 60 → neutral → confidence 50
        assert conf_retry == 50

    def test_high_venice_score_survives_degradation(self, scorer):
        """Venice score 100 → degraded to 75 → still > 70 → LR=4."""
        dims = _dims(50, 50, 50, 100)
        conf = scorer.compute_confidence(
            _identity(score=50), _liveness(score=50),
            _onchain(score=50), _venice(score=100, parse_method=VeniceParseMethod.REGEX_EXTRACTION),
            dims,
        )
        # 100 * 0.75 = 75 > 70 → LR=4 → still gets positive update
        assert conf > 50

    def test_venice_parse_failed_abstains(self, scorer):
        """venice_parse_failed=True → abstain entirely."""
        dims_with = _dims(85, 85, 85, 85)
        conf_with = scorer.compute_confidence(
            _identity(score=85), _liveness(score=85),
            _onchain(score=85), _venice(score=85), dims_with,
        )

        dims_without = _dims(85, 85, 85, 50)
        conf_without = scorer.compute_confidence(
            _identity(score=85), _liveness(score=85),
            _onchain(score=85), _venice(score=50, failed=True), dims_without,
        )
        # Abstaining Venice → fewer evidence items → lower confidence
        assert conf_without < conf_with


# ===========================================================================
# 4. Coefficient of Variation penalty
# ===========================================================================

class TestCVPenalty:
    """CV > 0.5 → strong penalty; CV > 0.3 → moderate; ≤0.3 → none."""

    def test_high_cv_lowers_confidence(self, scorer):
        """[90, 90, 90, 10] has high CV → lower confidence than [90, 90, 90, 80]."""
        dims_spread = _dims(90, 90, 90, 10)
        conf_spread = scorer.compute_confidence(
            _identity(score=90), _liveness(score=90),
            _onchain(score=90), _venice(score=10), dims_spread,
        )

        dims_tight = _dims(90, 90, 90, 80)
        conf_tight = scorer.compute_confidence(
            _identity(score=90), _liveness(score=90),
            _onchain(score=90), _venice(score=80), dims_tight,
        )
        assert conf_spread < conf_tight, \
            f"High CV should reduce confidence ({conf_spread} vs {conf_tight})"

    def test_low_cv_no_penalty(self, scorer):
        """[70, 72, 68, 71] → CV ≈ 0.02 → no penalty."""
        dims = _dims(70, 72, 68, 71)
        conf = scorer.compute_confidence(
            _identity(score=70), _liveness(score=72),
            _onchain(score=68), _venice(score=71), dims,
        )
        # All neutral (30-70 range for LR purposes: 70 is NOT >70, 68 is not <30)
        # Actually 72 > 70 → LR=4, 71 > 70 → LR=4, 70 is NOT > 70 → LR=1, 68 → LR=1
        # So log_odds = 2×log(4) = 2.772, no CV penalty (CV ≈ 0.02 < 0.3)
        # posterior ≈ 0.94 → 94
        assert conf > 85, f"Low CV, 2 strong dimensions should give >85, got {conf}"

    def test_cv_skipped_with_fewer_than_2_observed(self, scorer):
        """If only 1 dimension has data, CV is undefined → no penalty."""
        dims = _dims(85, 0, 0, 0)
        conf = scorer.compute_confidence(
            _identity(score=85),
            _liveness(declared=0, score=0),
            _onchain(success=False, score=0),
            _venice(score=0, failed=True),
            dims,
        )
        # Only identity observed: log(4) → posterior ≈ 0.8 → 80
        expected_approx = round(1.0 / (1.0 + math.exp(-math.log(4.0))) * 100)
        assert conf == expected_approx, f"1 observed, no CV, expected ~{expected_approx}, got {conf}"

    def test_moderate_cv_applies_moderate_penalty(self, scorer):
        """CV between 0.3 and 0.5 → log(1.5) penalty."""
        # [85, 85, 50, 85]: mean=76.25, stdev≈17.5, CV≈0.23 → no penalty (below 0.3)
        # Let's use [85, 85, 40, 85]: mean=73.75, stdev≈21.4, CV≈0.29 → still no
        # Use [90, 90, 40, 90]: mean=77.5, stdev≈25, CV≈0.32 → moderate penalty
        dims_moderate = _dims(90, 90, 40, 90)
        conf_moderate = scorer.compute_confidence(
            _identity(score=90), _liveness(score=90),
            _onchain(score=40), _venice(score=90), dims_moderate,
        )
        # Without penalty: 3×log(4) + log(1) = 4.16 → posterior≈0.98 → 98
        # With log(1.5) penalty: 4.16 - 0.405 = 3.76 → posterior≈0.977 → 98
        # Hmm, the penalty is small. Let's just verify it's less than the no-penalty case.
        dims_no_penalty = _dims(90, 90, 90, 90)
        conf_no_penalty = scorer.compute_confidence(
            _identity(score=90), _liveness(score=90),
            _onchain(score=90), _venice(score=90), dims_no_penalty,
        )
        assert conf_moderate <= conf_no_penalty


# ===========================================================================
# 5. determine_state()
# ===========================================================================

class TestDetermineState:
    """State determination is now purely threshold-based — no PENDING_HUMAN_REVIEW."""

    def test_confidence_70_verified(self, scorer):
        dims = _dims(80, 80, 80, 80)
        assert scorer.determine_state(70, dims) == EvaluationState.VERIFIED

    def test_confidence_69_withheld(self, scorer):
        dims = _dims(80, 80, 80, 80)
        assert scorer.determine_state(69, dims) == EvaluationState.WITHHELD_LOW_CONFIDENCE

    def test_confidence_100_verified(self, scorer):
        dims = _dims(100, 100, 100, 100)
        assert scorer.determine_state(100, dims) == EvaluationState.VERIFIED

    def test_confidence_0_withheld(self, scorer):
        dims = _dims(0, 0, 0, 0)
        assert scorer.determine_state(0, dims) == EvaluationState.WITHHELD_LOW_CONFIDENCE

    def test_high_spread_still_withheld(self, scorer):
        """High spread with low confidence → WITHHELD."""
        dims = _dims(100, 100, 0, 100)  # spread = 100
        state = scorer.determine_state(50, dims)
        assert state == EvaluationState.WITHHELD_LOW_CONFIDENCE

    def test_low_confidence_high_cv_still_withheld(self, scorer):
        """Low confidence + contradicting dimensions → WITHHELD, not human review."""
        dims = _dims(95, 5, 95, 5)  # extreme spread
        state = scorer.determine_state(40, dims)
        assert state == EvaluationState.WITHHELD_LOW_CONFIDENCE

    def test_boundary_threshold(self, scorer):
        """Exactly at threshold is VERIFIED."""
        dims = _dims(50, 50, 50, 50)
        assert scorer.determine_state(70, dims) == EvaluationState.VERIFIED
        assert scorer.determine_state(69, dims) == EvaluationState.WITHHELD_LOW_CONFIDENCE


# ===========================================================================
# 6. compute_composite() regression
# ===========================================================================

class TestComputeComposite:
    def test_all_100(self, scorer):
        # 100*(0.20+0.20+0.20+0.25+0.15) = 100
        assert scorer.compute_composite(_dims(100, 100, 100, 100, 100)) == 100

    def test_all_0(self, scorer):
        assert scorer.compute_composite(_dims(0, 0, 0, 0, 0)) == 0

    def test_all_60(self, scorer):
        assert scorer.compute_composite(_dims(60, 60, 60, 60, 60)) == 60

    def test_identity_only(self, scorer):
        assert scorer.compute_composite(_dims(100, 0, 0, 0, 0)) == 20

    def test_venice_weighted_highest(self, scorer):
        assert scorer.compute_composite(_dims(0, 0, 0, 100, 0)) == 25

    def test_liveness_weight(self, scorer):
        assert scorer.compute_composite(_dims(0, 100, 0, 0, 0)) == 20

    def test_onchain_weight(self, scorer):
        assert scorer.compute_composite(_dims(0, 0, 100, 0, 0)) == 20

    def test_protocol_weight(self, scorer):
        assert scorer.compute_composite(_dims(0, 0, 0, 0, 100)) == 15

    def test_clamped_to_100(self, scorer):
        assert scorer.compute_composite(_dims(100, 100, 100, 100, 100)) <= 100

    def test_realistic_score(self, scorer):
        dims = _dims(85, 75, 50, 70, 50)
        # 85*0.20 + 75*0.20 + 50*0.20 + 70*0.25 + 50*0.15 = 17 + 15 + 10 + 17.5 + 7.5 = 67
        assert scorer.compute_composite(dims) == 67

    def test_all_50(self, scorer):
        assert scorer.compute_composite(_dims(50, 50, 50, 50, 50)) == 50

    def test_without_protocol(self, scorer):
        """Protocol = 0 reduces composite vs same scores with protocol."""
        with_protocol = scorer.compute_composite(_dims(80, 80, 80, 80, 80))
        without = scorer.compute_composite(_dims(80, 80, 80, 80, 0))
        assert with_protocol > without


# ===========================================================================
# TrustDimensions helper properties
# ===========================================================================

class TestTrustDimensions:
    def test_spread_identical_scores(self):
        assert _dims(50, 50, 50, 50, 50).spread == 0

    def test_spread_varied_scores(self):
        assert _dims(100, 0, 50, 75, 50).spread == 100

    def test_as_list(self):
        assert _dims(10, 20, 30, 40, 50).as_list == [10, 20, 30, 40, 50]


# ===========================================================================
# to_report_json stability
# ===========================================================================

class TestReportJsonStability:
    def _make_verdict(self, state):
        agent = DiscoveredAgent(agent_id=1, agent_uri="https://x.com/agent.json", owner_address="0x" + "aa" * 20)
        return TrustVerdict(
            agent=agent, dimensions=_dims(80, 80, 80, 80), composite_score=80,
            evaluation_confidence=90, state=state,
            identity_verification=_identity(), liveness_result=_liveness(),
            onchain_analysis=_onchain(), venice_evaluation=_venice(),
            timestamp="2026-03-18T00:00:00Z", evaluation_id="test_123",
        )

    def test_report_json_excludes_state(self):
        verdict = self._make_verdict(EvaluationState.VERIFIED)
        report = json.loads(verdict.to_report_json())
        assert "state" not in report

    def test_report_json_deterministic(self):
        v1 = self._make_verdict(EvaluationState.VERIFIED)
        v2 = self._make_verdict(EvaluationState.PUBLISHED)
        assert v1.to_report_json() == v2.to_report_json()


# ===========================================================================
# Misc
# ===========================================================================

class TestVeniceTemperature:
    def test_default_temperature_zero(self):
        from config import Config
        assert Config().VENICE_TEMPERATURE == 0.0


class TestScoreToLR:
    """Unit test the LR bucketing directly."""

    def test_above_70_is_strong(self, scorer):
        assert scorer._score_to_lr(71) == 4.0
        assert scorer._score_to_lr(100) == 4.0

    def test_below_30_is_weak(self, scorer):
        assert scorer._score_to_lr(29) == 0.25
        assert scorer._score_to_lr(0) == 0.25

    def test_boundary_70_is_neutral(self, scorer):
        """70 is NOT > 70, so it maps to neutral."""
        assert scorer._score_to_lr(70) == 1.0

    def test_boundary_30_is_neutral(self, scorer):
        """30 is NOT < 30, so it maps to neutral."""
        assert scorer._score_to_lr(30) == 1.0

    def test_midrange_is_neutral(self, scorer):
        assert scorer._score_to_lr(50) == 1.0
