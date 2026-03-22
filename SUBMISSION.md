# Sentinel — Autonomous Agent Trust Oracle

**Track:** ERC-8004 "Agents With Receipts" · Venice "Private Agents, Trusted Actions"
**Live:** https://sentinel-trust-oracle-production.up.railway.app
**Dashboard:** https://sentinel-trust-oracle-production.up.railway.app/dashboard

---

## The Problem

The ERC-8004 ecosystem has a trust vacuum. Any wallet can register an agent identity. Any developer can point to a manifest. There is no mechanism to distinguish a well-built, production-grade agent from a placeholder that registered to game token incentives.

As the registry grows, agents calling other agents — and users relying on them — have no way to answer the question: *should I trust this agent?*

Sentinel answers it. Autonomously. On-chain. With proof.

---

## What Sentinel Does

Sentinel is an autonomous trust oracle that:

1. **Discovers** newly registered ERC-8004 agents from Identity Registry events
2. **Evaluates** each agent across 5 dimensions — identity completeness, endpoint liveness, on-chain history, Venice trust analysis, and protocol compliance
3. **Publishes** a trust score to the ERC-8004 Reputation Registry via `giveFeedback()`
4. **Attests** the full evaluation as a tamper-proof EAS attestation on Base
5. **Withholds** publication when confidence is insufficient — the system abstains rather than guesses

Every trust decision is a receipt. Every receipt is verifiable on-chain.

---

## The Receipts — Verifiable On Base Mainnet

**15 evaluations published** with EAS attestation UIDs. Each one is independently verifiable:

| Agent | Score | EAS Attestation |
|-------|-------|-----------------|
| #1    | 67    | [0xeb35a035...](https://base.easscan.org/attestation/view/0xeb35a03568d12b667ffa8e318b4a8f94759e491f432b7b686e3de19ba76993d9) |
| #5    | 71    | [0x1c1f8b52...](https://base.easscan.org/attestation/view/0x1c1f8b52884cd769872c0f21654678fa006092ae4abc4038650c05bdb0d16b7f) |
| #10   | 72    | [0x481e14ea...](https://base.easscan.org/attestation/view/0x481e14eaf7b8892cac40fe748d4013141d5dec5404082f107519bfd567f54899) |
| #20   | 72    | [0x40458ac4...](https://base.easscan.org/attestation/view/0x40458ac47a2233fcf835af007e9a260d093a960388f058c4a487253b46a4de13) |
| #50   | 70    | [0x216cc1f7...](https://base.easscan.org/attestation/view/0x216cc1f7911c0c7040f93e7801470f2ef49f0dcf6d3132afbad67fe045f0d6bd) |
| #100  | 72    | [0x75393efc...](https://base.easscan.org/attestation/view/0x75393efcc86ac7e4d63e40883b158aa55876f286d178e51b381089be4acdaa7d) |
| #35642| 100   | [0xd739a160...](https://base.easscan.org/attestation/view/0xd739a1601d9d090d66f07948d6fa37190383a5a92d7784b5864bb2596d1fd81b) |
| #35680| 89    | [0xed63c7bf...](https://base.easscan.org/attestation/view/0xed63c7bfc489809cf3caa595bffeccbe939b069ffb61aad6165099e320651e48) |
| #35683| 95    | [0x2c54bb89...](https://base.easscan.org/attestation/view/0x2c54bb89096240647a33f427220ac4ac621ffacb658a25640a9d8f2adef81a82) |

**EAS Schema UID:** `0x86402407ac6d3c823ea3bb44300b82011aa840ac19ef64c6f228b12fc4d60b87`

**100 agents evaluated total** — 15 published (trust confirmed), 54 abstained (insufficient evidence), 31 verified but not yet published. The 54 abstentions are not failures — they are the system working correctly. Agents with unverifiable identity, dead endpoints, or thin wallets don't deserve a receipt.

---

## All Three ERC-8004 Registries

Most ERC-8004 projects touch one registry. Sentinel uses all three:

| Registry | What Sentinel Does |
|----------|--------------------|
| **Identity Registry** | Reads agent manifests from registered URIs; validates against ERC-8004 spec; resolves IPFS/HTTP; detects duplicates |
| **Reputation Registry** | Writes `giveFeedback()` to publish trust scores on-chain; reads existing scores with HHI sybil analysis |
| **Validation Registry** | Issues `validationRequest` / reads `validationResponse` for challenge-response dispute resolution |

Sentinel is also a registered ERC-8004 agent itself — it registered its own identity on the protocol it evaluates, and exposes its capabilities as a manifest that other agents can verify.

---

## The Venice Loop — Private Cognition, Public Proof

Venice's role in Sentinel is architecturally precise, not decorative.

**The problem with additive LLM scoring:** If Venice contributes 25% weight alongside identity (30%), liveness (25%), and on-chain (20%), it double-counts. Venice sees the same signals as the other dimensions and synthesizes them — adding it as an independent dimension inflates confidence.

**Sentinel's solution:** Venice is a **multiplier on the base composite score**, not an additive dimension:

```
base = (identity × 0.30) + (liveness × 0.25) + (onchain × 0.25) + (protocol × 0.20)
multiplier = clamp(0.3 + 1.4 × (venice/100), 0.3, 1.5)
composite = base × multiplier
```

- Venice score of 50 (neutral) → multiplier 1.0 → no effect on base
- Venice score of 80 (high trust) → multiplier 1.42 → significant boost
- Venice score of 20 (fraud signal) → multiplier 0.58 → score halved, and veto cap at 30

**The privacy loop is the core design:**
- Venice processes the full agent context — manifest content, endpoint behaviour, on-chain patterns — with zero data retention
- The trust reasoning stays private. No agent can reconstruct why they received their score or reverse-engineer how to game it
- Only the score leaves Venice — written permanently on-chain via EAS, publicly verifiable forever

**Veto conditions:**
- Venice score < 20 → composite capped at 30 regardless of other dimensions (fraud signal)
- Identity score < 15 → composite capped at 35 (unverifiable identity)

This means a fraudulent agent cannot compensate with a high on-chain score or a polished manifest. The veto is absolute.

---

## Anti-Gaming Architecture

Trust oracles are worthless if they can be gamed. Sentinel is designed specifically to resist manipulation:

**1. HHI Sybil Detection**
The Herfindahl-Hirschman Index measures reviewer concentration on the Reputation Registry. If one wallet dominates an agent's reputation (HHI > 2500), a continuous penalty is applied:

```
penalty = 15 × (HHI - 1000) / 9000  [clamped to 0-15 points]
```

A smooth function, not a hard threshold — no cliff-edge gaming.

**2. Venice Correlation Discount**
In the confidence model, Venice log-odds are discounted by 0.6× because Venice synthesizes the same signals as other dimensions. Without this, confidence would be artificially inflated for any agent that passes Venice.

**3. Bayesian Log-Odds Confidence**
Confidence is not an average. It is a posterior probability derived from continuous likelihood ratios:

```
LR(score) = exp(log(8) × (score - 50) / 50)   # range [0.125, 8.0]
```

Dimensions with missing data abstain — they don't contribute neutral 0.5. A single strong signal and four unknowns does not produce 80% confidence. It produces ~72% confidence on limited evidence.

**4. CV Penalty for Spread**
When dimension scores are inconsistent (coefficient of variation > 0.3), confidence is penalised by -log(1.5). When CV > 0.5, penalised by -log(3). A polished manifest combined with dead endpoints and a thin wallet is a red flag, not an average.

---

## Protocol Compliance — Real MCP Handshake

During evaluation of agent #35691 (`microlink-io.vercel.app`), Sentinel successfully completed a live MCP `initialize` handshake:

```
POST https://microlink-io.vercel.app/mcp
← protocolVersion: "2025-03-26"
```

This is the first independently verified MCP protocol handshake in the Sentinel dataset. It demonstrates that the handshake implementation is real and functional — not simulated.

Protocol compliance scoring:
- **100**: Live MCP `initialize` handshake confirmed (protocolVersion returned)
- **80+**: Full ERC-8004 manifest compliance (type URI + skills + domains + task_categories)
- **45-65**: Partial ERC-8004 compliance (type URI or skills declared)
- **35**: HTTP infrastructure only
- **0**: No protocol signals

---

## MCP Server — Agent-to-Agent Trust

Sentinel exposes itself as an MCP server via stdio transport. Other agents can call Sentinel to verify agents before interacting with them:

```python
# Another agent calls Sentinel via MCP
result = await mcp_client.call_tool("verify_agent", {"agent_id": 42})
# → {"trust_score": 76, "confidence": 89, "attestation_uid": "0x..."}
```

**Available MCP tools:**
- `verify_agent(agent_id)` — full evaluation pipeline
- `check_reputation(agent_id)` — read existing scores with HHI + sybil flag
- `get_trust_chain(agent_id)` — score, confidence, attestation UID, timestamp
- `compute_transitive_trust(requester_id, target_id)` — Subjective Logic discount operator (Jøsang 2016): derived trust when you trust an agent that trusts another

---

## x402 Micropayment Gate

On Base Mainnet, the `/api/evaluate` endpoint is gated behind a 0.01 USDC payment:

```
POST /api/evaluate  →  402 Payment Required
X-Payment: base64({"txHash": "0x..."})
POST /api/evaluate  →  200 OK  (after payment verified)
```

Payment verification: Sentinel reads the USDC Transfer event from the tx receipt and confirms the transfer went to the operator address with value ≥ 10,000 (0.01 USDC, 6 decimals). Replay protection via used-hash set.

---

## Architecture

```
ERC-8004 Identity Registry
        ↓ discover (block scan)
   agent_discovery.py
        ↓
   orchestrator.py  ←── pipeline coordinator
    ├── agent_verifier.py    → fetch manifest, validate, score identity
    ├── liveness_checker.py  → HTTP probe + MCP initialize handshake
    ├── onchain_analyzer.py  → tx count, balance, ENS, HHI reputation
    ├── venice.py            → private trust analysis (Qwen3-235B, temp=0.4)
    └── scorer.py            → Bayesian composite + confidence
        ↓ if confidence ≥ 70%
   blockchain.py
    ├── ReputationRegistry.giveFeedback()  → score on-chain
    └── EAS.attest()                       → tamper-proof receipt
        ↓
   dashboard/  ←── live results, radar charts, EAS links
```

**Key files:**

| File | Role |
|------|------|
| `orchestrator.py` | 9-stage pipeline state machine |
| `scorer.py` | Venice multiplier + Bayesian log-odds confidence |
| `liveness_checker.py` | HTTP liveness + real MCP initialize handshake |
| `onchain_analyzer.py` | Wallet analysis + HHI sybil detection + ENS |
| `venice.py` | 4-layer parse fallback, temp=0.4, signal-driven prompt |
| `blockchain.py` | ERC-8004 Identity/Reputation/Validation + EAS |
| `mcp_server.py` | MCP stdio server with transitive trust |
| `api.py` | REST API + x402 payment gate |

---

## What the Numbers Mean

```
Agent #35642  score=100  confidence=97  state=PUBLISHED
  Identity: 100  Liveness: 67  On-chain: 83  Venice: 76  Protocol: 45
  EAS: 0xd739a160...
```

Venice score of 76 → multiplier 1.364 → base 73.5 × 1.364 = 100 (capped). Confidence 97% from 5 observed dimensions, all above the 50-point threshold. This agent earned its 100.

```
Agent #35743  score=17  confidence=61  state=WITHHELD
  Identity: 0  Liveness: 0  On-chain: 74  Venice: 45  Protocol: 0
```

Identity fetch failed. Zero live endpoints. Two dimensions observed, three abstained. Identity veto fires: composite capped at 35. Confidence 61% — not enough to publish. The wallet has an ENS name (`0xultravioleta.eth`) and 74 on-chain points, but those alone don't constitute trust. Correct decision.

---

## Running Sentinel

```bash
git clone <repo>
pip install -r requirements.txt
cp .env.example .env
# Fill: OPERATOR_PRIVATE_KEY, EVALUATOR_PRIVATE_KEY, VENICE_API_KEY

# Register Sentinel's own ERC-8004 identity
python3 main.py register --agent-uri https://your-host/agent.json

# Register EAS schema (one-time)
python3 main.py register-schema

# Discover and evaluate agents on Base Mainnet
python3 main.py --mainnet discover --max-agents 10

# Evaluate a specific agent
python3 main.py --mainnet manual --agent-id 35642

# Start MCP server (agent-to-agent trust)
python3 main.py mcp-server
```

---

## Contract Addresses

| Network | Identity Registry | Reputation Registry | Validation Registry |
|---------|-------------------|---------------------|---------------------|
| Base Mainnet | `0x8004A169...432` | `0x8004BAa1...b63` | `0x8004Cc84...B58` |
| Base Sepolia | `0x8004A818...D9e` | `0x8004B663...713` | `0x8004Cb1B...272` |

EAS (both networks): `0x4200000000000000000000000000000000000021`
EAS Schema UID: `0x86402407ac6d3c823ea3bb44300b82011aa840ac19ef64c6f228b12fc4d60b87`
