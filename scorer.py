"""5-dimension trust scoring engine with Bayesian confidence and CV-based disagreement penalty.

Likelihood ratio model uses a continuous exponential function rather than flat buckets,
so score=99 and score=71 produce different LRs (not identical as in a 3-bucket model).

  LR(s) = exp(k * (s - 50) / 50)   where k = log(LR_MAX) = log(8) ≈ 2.08

This gives:
  s=100 → LR ≈ 8.0   (strong trust signal)
  s= 75 → LR ≈ 2.8
  s= 50 → LR = 1.0   (neutral — no update)
  s= 25 → LR ≈ 0.35
  s=  0 → LR ≈ 0.125 (strong distrust signal)

The function is monotone, symmetric around 50, and bounded in [1/LR_MAX, LR_MAX].
"""
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

# Continuous LR model: LR(s) = exp(k * (s-50)/50), k = log(LR_MAX)
# LR_MAX = 8.0 → at score=100, LR=8; at score=0, LR=1/8=0.125
_LR_K = math.log(8.0)

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
        """Composite score: 4-dimension weighted base × Venice multiplier, with veto floor.

        Architecture:
        - Base (4 dims, sum=1.0): identity 30%, liveness 25%, onchain 25%, protocol 20%
        - Venice applied as a score multiplier centered at 50, range [0.3, 1.5]:
            multiplier = clamp(0.3 + 1.4 × venice/100, 0.3, 1.5)
          This prevents high base scores from compensating for fraud signals.
          Venice synthesizes all other dimensions — treating it as an additive
          weight would double-count identity+liveness+onchain evidence.
        - Veto floor: if Venice < threshold or identity < threshold, composite is
          hard-capped regardless of other dimensions (closes compensation attack).
        """
        # --- Base composite (4 dimensions, Venice excluded) ---
        base = (
            dimensions.identity_completeness * config.WEIGHT_IDENTITY   # 0.30
            + dimensions.endpoint_liveness   * config.WEIGHT_LIVENESS   # 0.25
            + dimensions.onchain_history     * config.WEIGHT_ONCHAIN    # 0.25
            + dimensions.protocol_compliance * config.WEIGHT_PROTOCOL   # 0.20
        )

        # --- Venice multiplier ---
        # f(0)=0.30  f(50)=1.00  f(100)=1.70 → clamped to [0.3, 1.5]
        venice = dimensions.venice_trust_analysis
        multiplier = max(
            config.VENICE_MULTIPLIER_MIN,
            min(config.VENICE_MULTIPLIER_MAX, 0.3 + 1.4 * (venice / 100.0)),
        )
        raw = base * multiplier
        composite = max(0, min(100, round(raw)))

        # --- Veto floor (hard caps for critical fraud signals) ---
        veto_cap = 100
        if venice < config.VETO_VENICE_THRESHOLD:
            veto_cap = min(veto_cap, config.VETO_COMPOSITE_CAP_VENICE)
            logger.warning(
                "Venice veto: venice=%d < %d threshold → cap=%d",
                venice, config.VETO_VENICE_THRESHOLD, config.VETO_COMPOSITE_CAP_VENICE,
            )
        if dimensions.identity_completeness < config.VETO_IDENTITY_THRESHOLD:
            veto_cap = min(veto_cap, config.VETO_COMPOSITE_CAP_IDENTITY)
            logger.warning(
                "Identity veto: identity=%d < %d threshold → cap=%d",
                dimensions.identity_completeness,
                config.VETO_IDENTITY_THRESHOLD, config.VETO_COMPOSITE_CAP_IDENTITY,
            )
        composite = min(composite, veto_cap)

        logger.info(
            "Composite: base=%.2f venice=%d mult=%.3f raw=%.2f veto_cap=%d final=%d",
            base, venice, multiplier, raw, veto_cap, composite,
        )
        return composite

    # --- Bayesian log-odds confidence model ---

    @staticmethod
    def _score_to_lr(score: int) -> float:
        """Map a dimension score (0-100) to a likelihood ratio via continuous exponential.

        LR(s) = exp(k * (s - 50) / 50), k = log(8)

        This is monotone and differentiable: score 99 > score 71, both above neutral,
        but produce distinct LRs rather than collapsing to the same bucket value.
        """
        return math.exp(_LR_K * (score - 50) / 50.0)

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

        # --- Venice (with correlation discount) ---
        # Venice synthesizes identity + liveness + onchain signals, so its evidence
        # partially overlaps with the other three dimensions already counted above.
        # Applying a 0.6 discount factor to its log-odds contribution avoids
        # inflating confidence by treating correlated evidence as independent.
        # Equivalent to: effective LR = lr^0.6 (dampened but directionally correct).
        _VENICE_CORRELATION_DISCOUNT = 0.6
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
            discounted_log_lr = _VENICE_CORRELATION_DISCOUNT * math.log(lr)
            log_odds += discounted_log_lr
            observed_scores.append(dimensions.venice_trust_analysis)
            logger.info(
                "Venice log-odds: full=%.3f discounted=%.3f (correlation_discount=%.1f)",
                math.log(lr), discounted_log_lr, _VENICE_CORRELATION_DISCOUNT,
            )

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
