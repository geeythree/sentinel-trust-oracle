"""4-dimension trust scoring engine with Bayesian confidence and CV-based disagreement penalty."""
from __future__ import annotations

import logging
import math
import statistics

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

# Likelihood ratios encode a 2-bit information model:
#   strong evidence FOR trust, neutral (no update), strong evidence AGAINST.
_LR_STRONG = 4.0    # score > 70
_LR_NEUTRAL = 1.0   # score 30–70
_LR_WEAK = 0.25     # score < 30

# Venice parse-quality degradation factor for non-JSON_SCHEMA parse methods.
_VENICE_DEGRADATION = 0.75


class Scorer:
    """Compute composite trust scores and Bayesian confidence."""

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
            protocol_compliance=liveness.protocol_compliance_score,
        )
        logger.info(
            "Dimensions: identity=%d liveness=%d onchain=%d venice=%d protocol=%d spread=%d",
            dims.identity_completeness, dims.endpoint_liveness,
            dims.onchain_history, dims.venice_trust_analysis,
            dims.protocol_compliance, dims.spread,
        )
        return dims

    def compute_composite(self, dimensions: TrustDimensions) -> int:
        """Weighted composite score across 5 dimensions, clamped 0-100.

        Weight rationale (env-configurable via WEIGHT_* vars):
        - Identity 20%: Most gameable — anyone can fill manifest fields.
        - Liveness 20%: Requires real infrastructure to sustain.
        - On-chain 20%: Historical transaction record is expensive to forge.
        - Venice 25%: Cross-references ALL signals and detects inconsistencies.
          Highest weight because it synthesizes rather than measures a single axis.
        - Protocol 15%: MCP handshake compliance — verifies agent actually speaks
          the protocol, not just serves HTTP. Lowest weight as it's binary.
        """
        raw = (
            dimensions.identity_completeness * config.WEIGHT_IDENTITY       # 0.20
            + dimensions.endpoint_liveness * config.WEIGHT_LIVENESS         # 0.20
            + dimensions.onchain_history * config.WEIGHT_ONCHAIN            # 0.20
            + dimensions.venice_trust_analysis * config.WEIGHT_VENICE_TRUST # 0.25
            + dimensions.protocol_compliance * config.WEIGHT_PROTOCOL       # 0.15
        )
        composite = max(0, min(100, round(raw)))
        logger.info("Composite: raw=%.2f clamped=%d", raw, composite)
        return composite

    # --- Bayesian log-odds confidence model ---

    @staticmethod
    def _score_to_lr(score: int) -> float:
        """Map a dimension score (0-100) to a likelihood ratio."""
        if score > 70:
            return _LR_STRONG
        if score < 30:
            return _LR_WEAK
        return _LR_NEUTRAL

    def compute_confidence(
        self,
        identity: IdentityVerification,
        liveness: LivenessResult,
        onchain: OnchainAnalysis,
        venice: VeniceEvaluation,
        dimensions: TrustDimensions,
    ) -> int:
        """Bayesian log-odds confidence (0-100).

        Starts at log-odds 0 (50% prior). Each dimension with data updates the
        log-odds via its likelihood ratio. Missing data abstains (LR=1 → no update).
        A coefficient-of-variation penalty is applied for disagreeing dimensions.
        """
        log_odds = 0.0
        observed_scores: list[int] = []

        # --- Identity ---
        if identity.success:
            lr = self._score_to_lr(dimensions.identity_completeness)
            log_odds += math.log(lr)
            observed_scores.append(dimensions.identity_completeness)
        else:
            logger.info("Identity abstained (fetch failed)")

        # --- Liveness ---
        if liveness.endpoints_declared > 0:
            lr = self._score_to_lr(dimensions.endpoint_liveness)
            log_odds += math.log(lr)
            observed_scores.append(dimensions.endpoint_liveness)
        else:
            logger.info("Liveness abstained (0 endpoints declared)")

        # --- On-chain ---
        if onchain.success:
            lr = self._score_to_lr(dimensions.onchain_history)
            log_odds += math.log(lr)
            observed_scores.append(dimensions.onchain_history)
        else:
            logger.info("On-chain abstained (analysis failed)")

        # --- Venice ---
        if venice.venice_parse_failed:
            logger.info("Venice abstained (parse failed)")
        else:
            effective_score = venice.score
            if venice.parse_method != VeniceParseMethod.JSON_SCHEMA:
                effective_score = round(venice.score * _VENICE_DEGRADATION)
                logger.info(
                    "Venice degraded: raw=%d effective=%d (parse_method=%s)",
                    venice.score, effective_score, venice.parse_method.value,
                )
            lr = self._score_to_lr(effective_score)
            log_odds += math.log(lr)
            observed_scores.append(dimensions.venice_trust_analysis)

        # --- Protocol Declaration ---
        if dimensions.protocol_compliance > 0:
            lr = self._score_to_lr(dimensions.protocol_compliance)
            log_odds += math.log(lr)
            observed_scores.append(dimensions.protocol_compliance)
        else:
            logger.info("Protocol compliance abstained (no MCP endpoints)")

        # --- CV penalty (only if ≥2 observed dimensions) ---
        if len(observed_scores) >= 2:
            mean = statistics.mean(observed_scores)
            if mean > 0:
                stdev = statistics.stdev(observed_scores)
                cv = stdev / mean
                if cv > 0.5:
                    penalty = math.log(3)
                    log_odds -= penalty
                    logger.info("CV penalty: -log(3) (CV=%.3f > 0.5)", cv)
                elif cv > 0.3:
                    penalty = math.log(1.5)
                    log_odds -= penalty
                    logger.info("CV penalty: -log(1.5) (CV=%.3f > 0.3)", cv)

        # --- Convert to posterior probability ---
        posterior = 1.0 / (1.0 + math.exp(-log_odds))
        confidence = max(0, min(100, round(posterior * 100)))
        logger.info(
            "Confidence: log_odds=%.3f posterior=%.4f confidence=%d (observed=%d dims)",
            log_odds, posterior, confidence, len(observed_scores),
        )
        return confidence

    def determine_state(
        self,
        confidence: int,
        dimensions: TrustDimensions,
    ) -> EvaluationState:
        """Determine evaluation lifecycle state.

        Fully autonomous: uncertain verdicts are withheld, never delegated.
        - confidence >= 70 → VERIFIED (auto-publish)
        - confidence <  70 → WITHHELD_LOW_CONFIDENCE
        """
        if confidence >= config.CONFIDENCE_THRESHOLD:
            state = EvaluationState.VERIFIED
        else:
            state = EvaluationState.WITHHELD_LOW_CONFIDENCE
        logger.info(
            "State determination: confidence=%d -> %s",
            confidence, state.value,
        )
        return state
