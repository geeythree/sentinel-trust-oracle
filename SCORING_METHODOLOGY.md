# Sentinel Scoring Methodology

## Overview

Sentinel evaluates ERC-8004 agents across five independent trust dimensions, then fuses the evidence using two complementary models:

- **Composite score** (0–100): weighted average of dimension scores — "how trustworthy is this agent?"
- **Confidence** (0–100): Bayesian posterior probability — "how confident are we in this evaluation?"

High composite + high confidence → publish on-chain. High composite + low confidence → withhold (uncertain verdict).

---

## 1. Composite Score — Weighted Average

```
composite = 0.20 × identity + 0.20 × liveness + 0.20 × onchain + 0.25 × venice + 0.15 × protocol
```

| Dimension | Weight | Rationale |
|-----------|--------|-----------|
| Identity Completeness | 20% | Easiest to game — anyone can fill manifest fields. |
| Endpoint Liveness | 20% | Requires real infrastructure (servers, TLS certs, uptime). |
| On-chain History | 20% | Historical tx record is expensive to forge at scale. |
| Venice Trust Analysis | 25% | Synthesizes all other signals; detects cross-dimension inconsistencies. Highest weight. |
| Protocol Declaration | 15% | MCP handshake compliance — verifies agent actually speaks the protocol. Binary signal, lowest weight. |

The weights follow an AHP-inspired pairwise intuition: dimensions that are harder to fake or that integrate multiple signals receive higher weight. Venice is highest because it's the only dimension that reasons across the other four rather than measuring a single axis.

Weights are env-configurable (`WEIGHT_IDENTITY`, `WEIGHT_LIVENESS`, `WEIGHT_ONCHAIN`, `WEIGHT_VENICE_TRUST`, `WEIGHT_PROTOCOL`) and must sum to 1.0.

---

## 2. Confidence — Bayesian Log-Odds

### Model

Start with a **uniform prior** (log-odds = 0, i.e. 50% probability). Each dimension with available data contributes a likelihood ratio (LR) that shifts the log-odds:

```
log_odds = 0                         # prior: no information
log_odds += log(LR_identity)         # update with identity evidence
log_odds += log(LR_liveness)         # update with liveness evidence
log_odds += log(LR_onchain)          # update with on-chain evidence
log_odds += log(LR_venice)           # update with Venice evidence
log_odds += log(LR_protocol)         # update with protocol compliance evidence
log_odds -= CV_penalty               # penalize disagreement
posterior = 1 / (1 + exp(-log_odds)) # convert to probability
confidence = round(posterior × 100)  # scale to 0-100
```

### Likelihood Ratios

Each dimension score maps to a likelihood ratio encoding a 2-bit information model:

| Score Range | LR | log(LR) | Interpretation |
|-------------|-----|---------|---------------|
| > 70 | 4.0 | +1.386 | Strong evidence of trust |
| 30–70 | 1.0 | 0.000 | Neutral — no update |
| < 30 | 0.25 | −1.386 | Evidence against trust |

The LR values are symmetric: `4.0 × 0.25 = 1.0`, so one strong positive and one strong negative exactly cancel. This is deliberate — contradictory signals should produce uncertainty, not a false mean.

### Abstention Rule

If a dimension has **no data** (identity fetch failed, 0 endpoints declared, on-chain analysis failed, Venice parse failed), it uses LR = 1.0 (abstain). This is the correct Bayesian treatment: missing evidence provides no information, so it neither supports nor contradicts trust. The system does not penalize agents for data it couldn't collect.

### Venice Parse-Quality Degradation

Venice responses parsed via regex extraction or retry correction are less reliable than JSON schema-validated responses. For non-JSON_SCHEMA parse methods, the effective Venice score is reduced:

```
effective_score = score × 0.75
```

A Venice score of 80 parsed via regex becomes an effective 60, falling into the neutral LR bucket instead of the strong bucket. This prevents overconfident conclusions from unreliable LLM output.

---

## 3. CV Disagreement Penalty

The **coefficient of variation** (CV = σ/μ over observed dimensions) measures how much the dimensions disagree. High disagreement signals that the evidence is contradictory, which should reduce confidence regardless of the direction of the log-odds.

| CV | Penalty | Effect |
|----|---------|--------|
| > 0.5 | −log(3) ≈ −1.099 | Strong disagreement — substantial confidence reduction |
| > 0.3 | −log(1.5) ≈ −0.405 | Moderate disagreement — mild confidence reduction |
| ≤ 0.3 | 0 | Dimensions are consistent — no penalty |

CV is computed only over dimensions that contributed data (not abstained). If fewer than 2 dimensions have data, CV is undefined and no penalty is applied.

**Why CV instead of spread (max − min)?** Spread is scale-sensitive: a spread of 50 on a mean of 90 (scores: 65, 90, 90, 95) is very different from spread 50 on a mean of 55 (scores: 30, 55, 55, 80). CV normalizes by the mean, making the disagreement measure independent of the score level.

---

## 4. State Determination

```
confidence >= 70 → VERIFIED (auto-publish to chain)
confidence <  70 → WITHHELD_LOW_CONFIDENCE
```

Sentinel is fully autonomous. Uncertain verdicts are **withheld**, never delegated to human review. The system prefers withholding a potentially valid agent over publishing a potentially wrong score. Agents with withheld verdicts can be re-evaluated when more data becomes available (new transactions, endpoints come online, etc.).

---

## 5. Alternative Approaches Considered

**Subjective Logic (Jøsang)** models belief, disbelief, and uncertainty as a triple on a Dirichlet distribution. It's more expressive than Bayesian log-odds because it explicitly represents "I don't know" as a separate parameter rather than collapsing it into a 50% posterior.

We chose Bayesian log-odds because:

1. **Simpler**: two parameters (log-odds + observed count) vs. three (b, d, u per dimension).
2. **Independence assumption holds**: the four dimensions are structurally different layers (manifest content, HTTP checks, blockchain state, LLM synthesis). They don't share failure modes, so treating them as independent evidence sources is reasonable.
3. **Abstention is natural**: LR = 1.0 for missing data is the standard Bayesian no-evidence update, and it produces the correct behavior (posterior unchanged) without needing a separate uncertainty parameter.
4. **CV captures disagreement**: the main benefit of Subjective Logic's uncertainty parameter is detecting conflicting evidence. CV over observed scores achieves the same goal with less machinery.

If Sentinel later needs to model correlated failure modes (e.g., Venice score depending on identity data quality), Subjective Logic or a graphical model would be the appropriate upgrade.

---

## 6. Protocol Declaration Scoring

| Signal | Score |
|--------|-------|
| MCP transport (stdio/SSE) + live HTTP endpoints | 100 |
| MCP transport declared (no live HTTP) | 90 |
| MCP metadata (mcpVersion, supportedProtocols) | 80 |
| HTTP endpoints only (no MCP signals) | 50 |
| No endpoints | 0 (abstains from confidence) |

This dimension checks whether an agent's manifest **declares** MCP-compatible transport or metadata — it does not perform an actual MCP `initialize` handshake. It is a declaration check, not a protocol verification. Agents with `protocol_compliance = 0` abstain from the confidence model (LR = 1.0).

---

## 7. Anti-Gaming Measures

### Identity Score Hardening
- **Placeholder name detection**: Generic names ("test", "agent", "bot", "demo") score 5/10 instead of 10/10.
- **Description quality**: Must be ≥ 50 chars with ≥ 3 distinct words for full score.
- **Filler domain blocklist**: Endpoints using `example.com`, `localhost`, `test.com`, `httpbin.org` get 0 for the non-filler bonus.
- **Duplicate manifest detection**: Manifests are SHA-256 hashed; duplicates across agents are flagged.

### On-chain Score Components (50–100)
- Baseline: 50 (no penalty for new wallets)
- Transaction count: 7 buckets, max +25
- Balance: 5 tiers, max +12
- Existing reputation: 3 tiers, max +13
- **Contract code detection**: +10 (address has deployed bytecode — strong developer signal)

---

## 8. Input Hash (Tamper-Proof Attestation)

Each evaluation computes a SHA-256 hash of all inputs:

```
SHA-256(JSON.stringify({
    manifest, identity_score, liveness_score,
    endpoints_declared, endpoints_live,
    onchain_score, tx_count, balance_eth,
    venice_score, venice_parse_method
}, sort_keys=True))
```

This hash is included in the dashboard output and report JSON, enabling independent verification that the score was derived from specific inputs. Anyone can recompute the hash from the raw inputs to verify the evaluation wasn't tampered with.
