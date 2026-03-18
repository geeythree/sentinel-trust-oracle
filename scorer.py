"""4-dimension trust scoring engine, confidence calculation, and state determination."""
from __future__ import annotations

import logging

from config import config
from models import (
    EvaluationState,
    IdentityVerification,
    LivenessResult,
    OnchainAnalysis,
    TrustDimensions,
    VeniceEvaluation,
    VeniceParseMethod,
)

logger = logging.getLogger(__name__)


class Scorer:
    """Compute composite trust scores and evaluation confidence."""

    def compute_dimensions(
        self,
        identity: IdentityVerification,
        liveness: LivenessResult,
        onchain: OnchainAnalysis,
        venice: VeniceEvaluation,
    ) -> TrustDimensions:
        """Assemble dimension scores from individual module outputs."""
        dims = TrustDimensions(
            identity_completeness=identity.identity_score,
            endpoint_liveness=liveness.liveness_score,
            onchain_history=onchain.onchain_score,
            venice_trust_analysis=venice.score,
        )
        logger.info(
            "Dimensions: identity=%d liveness=%d onchain=%d venice=%d spread=%d",
            dims.identity_completeness, dims.endpoint_liveness,
            dims.onchain_history, dims.venice_trust_analysis, dims.spread,
        )
        return dims

    def compute_composite(self, dimensions: TrustDimensions) -> int:
        """Weighted composite: 20/25/25/30. Clamped 0-100."""
        raw = (
            dimensions.identity_completeness * config.WEIGHT_IDENTITY       # 0.20
            + dimensions.endpoint_liveness * config.WEIGHT_LIVENESS         # 0.25
            + dimensions.onchain_history * config.WEIGHT_ONCHAIN            # 0.25
            + dimensions.venice_trust_analysis * config.WEIGHT_VENICE_TRUST # 0.30
        )
        composite = max(0, min(100, round(raw)))
        logger.info("Composite: raw=%.2f clamped=%d", raw, composite)
        return composite

    def compute_confidence(
        self,
        identity: IdentityVerification,
        liveness: LivenessResult,
        onchain: OnchainAnalysis,
        venice: VeniceEvaluation,
        dimensions: TrustDimensions,
    ) -> int:
        """Compute evaluation_confidence (0-100)."""
        conf = 0

        # Identity verified (manifest fetched and valid)
        if identity.success and not identity.fields_missing:
            conf += 25
        elif identity.success:
            conf += 15  # partial manifest

        # Liveness check completed
        if liveness.success and liveness.endpoints_declared > 0:
            if liveness.endpoints_dead == 0:
                conf += 25  # all endpoints respond
            else:
                conf += 15  # some endpoints dead

        # Venice analysis
        conf += self._venice_confidence_contribution(venice)

        # On-chain data available
        if onchain.success and onchain.transaction_count > 0:
            conf += 25
        elif onchain.success:
            conf += 15  # wallet exists but no history
        else:
            logger.info("On-chain analysis failed — 0 confidence contribution")

        # Spread penalties
        spread = dimensions.spread
        if spread > config.SPREAD_HUMAN_REVIEW_THRESHOLD:  # > 50
            conf -= 30
            logger.info("Spread penalty: -30 (spread=%d > %d)", spread, config.SPREAD_HUMAN_REVIEW_THRESHOLD)
        elif spread > config.SPREAD_PENALTY_THRESHOLD:  # > 30
            conf -= 15
            logger.info("Spread penalty: -15 (spread=%d > %d)", spread, config.SPREAD_PENALTY_THRESHOLD)

        confidence = max(0, min(100, conf))
        logger.info("Confidence: %d", confidence)
        return confidence

    def _venice_confidence_contribution(self, v: VeniceEvaluation) -> int:
        """Compute confidence contribution from a Venice evaluation."""
        if v.venice_parse_failed:
            return 0
        if v.parse_method == VeniceParseMethod.JSON_SCHEMA:
            return 25
        if v.parse_method in (VeniceParseMethod.REGEX_EXTRACTION,
                              VeniceParseMethod.RETRY_CORRECTION):
            return 15
        return 0

    def determine_state(
        self,
        confidence: int,
        dimensions: TrustDimensions,
    ) -> EvaluationState:
        """Determine evaluation lifecycle state.

        Decision logic:
        - confidence >= 70 -> VERIFIED (auto-publish)
        - confidence < 70 AND spread <= 50 -> WITHHELD_LOW_CONFIDENCE
        - confidence < 70 AND spread > 50 -> PENDING_HUMAN_REVIEW
        """
        if confidence >= config.CONFIDENCE_THRESHOLD:
            state = EvaluationState.VERIFIED
        elif dimensions.spread > config.SPREAD_HUMAN_REVIEW_THRESHOLD:
            state = EvaluationState.PENDING_HUMAN_REVIEW
        else:
            state = EvaluationState.WITHHELD_LOW_CONFIDENCE
        logger.info(
            "State determination: confidence=%d spread=%d -> %s",
            confidence, dimensions.spread, state.value,
        )
        return state
