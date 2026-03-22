# Sentinel

**Autonomous trust oracle for AI agents on Base.**

Sentinel discovers [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004) registered agents, verifies their identity and capabilities, privately evaluates trust signals via [Venice](https://venice.ai), and writes verifiable reputation scores on-chain via [EAS](https://attest.org) attestations.

[Live Dashboard](https://geeythree.github.io/sentinel-trust-oracle/) · [BaseScan](https://basescan.org)

---

## Why

ERC-8004 gave 20,000+ agents on-chain identity. But the Reputation and Validation registries are empty — no product verifies agent identities or populates trust data. MCP has [5+ CVEs](https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks) including CVSS 9.6 RCE. A2A Agent Cards are unsigned JSON anyone can forge.

**There is no standardized way for agents to verify each other before interacting.**

Sentinel fixes this by acting as an autonomous trust oracle — discovering agents, evaluating them across 4 dimensions, and publishing verifiable scores that other agents can query.

## Privacy-First Architecture

Sentinel's trust evaluation is designed for **privacy-preserving trust scoring** — the oracle produces verifiable on-chain scores without exposing any agent metadata to third parties.

| Principle | How |
|-----------|-----|
| **Private Inference** | All LLM evaluation runs through [Venice](https://venice.ai)'s no-data-retention inference. Agent manifests, endpoints, and wallet data are never stored by the LLM provider. |
| **No Data Logging** | Venice operates with zero data retention — prompts and completions are discarded after response generation. Sentinel never sends agent data to any other third-party API. |
| **On-chain Transparency** | Trust scores are published as EAS attestations on Base — publicly verifiable, but the raw evaluation inputs remain private. Only the final score, confidence, and dimension breakdown are on-chain. |
| **Local Processing** | Identity verification, liveness checking, and on-chain analysis all run locally. Only the trust synthesis step uses Venice's private LLM. |

This means agents can be evaluated without their owners' data being harvested, stored, or sold — a critical requirement for autonomous agent ecosystems.

### Why Venice Is Essential (Not Optional)

Venice isn't a convenience — it's the only component that can synthesize trust from ambiguous signals. The other three dimensions produce raw metrics (field counts, HTTP codes, tx counts). Venice interprets what those metrics *mean* together.

| Scenario | Without Venice | With Venice |
|----------|---------------|-------------|
| Agent has valid manifest + live endpoints but 0 tx history | Score: 45, no context | Score: 62 — Venice recognizes a newly deployed but legitimate agent |
| Agent has 500 txs + high balance but broken manifest | Score: 44, no context | Score: 35 — Venice flags the inconsistency as suspicious |
| Agent has secured endpoints (401/403) + partial manifest | Score: 50, ambiguous | Score: 71 — Venice understands that auth-protected APIs are a positive signal |

Removing Venice drops trust scoring to mechanical heuristics that can't distinguish a legitimate new agent from a dormant scam wallet. Venice provides the **interpretive layer** that makes trust scores meaningful — and it does so with zero data retention, ensuring agent privacy is never compromised.

## Why Sentinel Over Alternatives

Other approaches to agent trust exist, but each has a gap Sentinel fills:

| Approach | Limitation | Sentinel's Answer |
|----------|-----------|------------------|
| **Manual curation** (allowlists, directories) | Doesn't scale. 20,000+ ERC-8004 agents can't be hand-reviewed. | Fully autonomous — discovers, evaluates, and publishes without human intervention. |
| **Single-signal scoring** (just check if endpoint responds) | Trivially gameable. A static server returning 200 passes. | 4-dimensional scoring — identity, liveness, on-chain history, and LLM-interpreted trust. Spoofing one dimension doesn't produce a high composite score. |
| **Centralized reputation APIs** | Single point of failure. Provider can censor, manipulate, or go offline. | Scores are published as EAS attestations on Base — immutable, verifiable, and queryable by anyone. |
| **LLM-only evaluation** | Prompt-injectable. Agent manifests could contain adversarial instructions. | Venice input is capped at 4KB and sanitized. LLM score is 30% of composite, not 100% — mechanical checks anchor the evaluation. |
| **Self-reported trust** (agents claim their own scores) | Obvious conflict of interest. | Evaluator wallet is separate from agent owner wallet. Sentinel uses a 3-wallet model to prevent self-scoring. |

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                          SENTINEL                                │
│                                                                  │
│   ① Discovery        ② Verification      ③ Liveness             │
│   ┌──────────┐       ┌──────────┐        ┌──────────┐           │
│   │ ERC-8004 │──────▶│ Fetch    │───────▶│ HTTP     │           │
│   │ Registry │       │ agent.json│       │ HEAD     │           │
│   │ events   │       │ IPFS/HTTPS│       │ per svc  │           │
│   └──────────┘       └──────────┘        └────┬─────┘           │
│                                               │                  │
│   ⑥ Publish          ⑤ Scoring            ④ Analysis            │
│   ┌──────────┐       ┌──────────┐        ┌────▼─────┐           │
│   │ ERC-8004 │◀──────│ Weighted │◀───────│ Venice   │           │
│   │ Reputation│      │ composite│        │ private  │           │
│   │ + EAS    │       │ + confid.│        │ eval +   │           │
│   │ attest   │       │          │        │ on-chain │           │
│   └──────────┘       └──────────┘        └──────────┘           │
└──────────────────────────────────────────────────────────────────┘
```

| Stage | What happens | Tool |
|-------|-------------|------|
| **Discovery** | Scan `Registered` events on the ERC-8004 Identity Registry | web3.py |
| **Verification** | Fetch `agent.json` from URI (HTTPS, IPFS, data:), validate manifest | requests + tenacity |
| **Liveness** | HTTP HEAD each declared service endpoint — 401/403 = secured = good | requests |
| **On-chain** | Wallet tx count, balance, existing reputation — no penalty for new wallets | web3.py |
| **Venice** | Private trust analysis with zero data retention | Venice API (Qwen3-235B) |
| **Publish** | Write score to Reputation Registry + EAS attestation | web3.py |

## Trust Dimensions

| Dimension | Weight | What it measures |
|-----------|--------|-----------------|
| Identity Completeness | 20% | Manifest fields, services declared, URI resolvable |
| Endpoint Liveness | 25% | Service endpoints responding (200, 401, 403 = alive) |
| On-chain History | 25% | Transaction count, balance, existing reputation |
| Venice Trust Analysis | 30% | Private LLM evaluation of overall trustworthiness |

Composite score: 0–100. Confidence score determines auto-publish vs human review.

## Quick Start

```bash
git clone https://github.com/geeythree/sentinel-trust-oracle.git
cd sentinel-trust-oracle

python3 -m venv env
source env/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Add: OPERATOR_PRIVATE_KEY, EVALUATOR_PRIVATE_KEY, VENICE_API_KEY
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPERATOR_PRIVATE_KEY` | Yes | — | Private key for ERC-8004 identity owner wallet |
| `EVALUATOR_PRIVATE_KEY` | Yes | — | Private key for reputation feedback wallet (must differ from operator) |
| `VENICE_API_KEY` | Yes | — | API key from [venice.ai](https://venice.ai) |
| `EAS_SCHEMA_UID` | No | `""` | EAS schema UID (run `register-schema` to get one) |
| `AUDITOR_PRIVATE_KEY` | No | `""` | Optional third wallet for self-reputation |
| `USE_TESTNET` | No | `true` | `true` for Base Sepolia, `false` for Base Mainnet |
| `BASE_RPC_URL` | No | `https://mainnet.base.org` | Base Mainnet RPC endpoint |
| `BASE_SEPOLIA_RPC_URL` | No | `https://sepolia.base.org` | Base Sepolia RPC endpoint |
| `BASESCAN_API_KEY` | No | `""` | BaseScan API key (optional, for enhanced on-chain analysis) |

### One-time setup

```bash
# Register EAS schema (save the UID to .env)
python3 main.py register-schema --testnet

# Register Sentinel's own identity
python3 main.py register --agent-uri <url-to-agent.json> --testnet
```

## Usage

```bash
# Discover and evaluate agents on Base Sepolia
python3 main.py discover --testnet --max-agents 5

# Evaluate a specific agent by ID
python3 main.py manual --agent-id 42

# Evaluate by owner address
python3 main.py manual --address 0x...

# Start MCP server (agent-to-agent verification)
python3 main.py mcp-server

# Mainnet
python3 main.py discover --mainnet
```

### MCP Integration

Sentinel exposes two tools via [MCP](https://modelcontextprotocol.io) (stdio transport):

| Tool | Description |
|------|-------------|
| `verify_agent` | Full trust verification pipeline — returns score, confidence, attestation |
| `check_reputation` | Read existing reputation from ERC-8004 Reputation Registry |

Other agents can verify trust before interacting:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async with stdio_client(StdioServerParameters(
    command="python3", args=["mcp_server.py"]
)) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("verify_agent", {"agent_id": 42})
```

A complete demo client is included — run `python3 dummy_client_agent.py --agent-id 1`.

### Example Output

```
SENTINEL TRUST EVALUATION SUMMARY
================================================================================
Agent ID    Score  Conf    I    L    O    V  State                     TX
--------------------------------------------------------------------------------
#42            77    80   80  100   60   70  PUBLISHED                 0xabc123...
#43            45    55   60    0   50   40  WITHHELD_LOW_CONFIDENCE   N/A
================================================================================
Total: 2 agents evaluated
```

## Architecture

```
main.py                 CLI entry point (6 modes)
orchestrator.py         Pipeline coordinator + state machine
agent_discovery.py      ERC-8004 Identity Registry event scanning
agent_verifier.py       Fetch + validate agent.json (HTTPS/IPFS/data:)
liveness_checker.py     HTTP endpoint liveness checking
onchain_analyzer.py     Wallet history + existing reputation
venice.py               Venice API with 4-layer parse fallback
scorer.py               Weighted composite + confidence calculation
blockchain.py           web3.py wrapper for ERC-8004 + EAS
mcp_server.py           MCP server (verify_agent, check_reputation)
models.py               All dataclasses — single source of truth
config.py               Deferred config creation from env vars
logger.py               Append-mode JSON Lines logger
exceptions.py           Exception hierarchy
```

### State Machine

```
DISCOVERED → PLANNING → FETCH_IDENTITY → CHECK_LIVENESS
  → ON_CHAIN_ANALYSIS → VENICE_TRUST → SCORING → VERIFYING
  → PUBLISHING → PUBLISHED

Terminal: WITHHELD_LOW_CONFIDENCE | PENDING_HUMAN_REVIEW | FAILED
```

**Auto-publish** when confidence ≥ 70. **Human review** when confidence < 70 and dimension spread > 50.

## Contracts

| Network | Identity Registry | Reputation Registry | EAS |
|---------|-------------------|---------------------|-----|
| Base Mainnet | [`0x8004...9432`](https://basescan.org/address/0x8004A169FB4a3325136EB29fA0ceB6D2e539a432) | [`0x8004...9B63`](https://basescan.org/address/0x8004BAa17C55a88189AE136b182e5fdA19dE9b63) | [`0x4200...0021`](https://basescan.org/address/0x4200000000000000000000000000000000000021) |
| Base Sepolia | [`0x8004...BD9e`](https://sepolia.basescan.org/address/0x8004A818BFB912233c491871b3d84c89A494BD9e) | [`0x8004...8713`](https://sepolia.basescan.org/address/0x8004B663056A597Dffe9eCcC1965A193B7388713) | [`0x4200...0021`](https://sepolia.basescan.org/address/0x4200000000000000000000000000000000000021) |

## Known Limitations & Future Work

| Area | Current State | Next Step |
|------|--------------|-----------|
| **Liveness checking** | HTTP HEAD/GET per endpoint — verifies reachability and auth status. | Add TLS certificate validation, response schema checks, and semantic verification (does the endpoint behave like an AI agent or just return 200?). |
| **Sybil resistance** | Separate evaluator wallet prevents self-scoring, but a determined attacker could register many agents. | Weight reputation by evaluator stake or use attestation graphs to detect evaluation rings. |
| **Discovery scale** | Scans `Registered` events via `eth_getLogs` — O(n blocks). | Migrate to a Graph Protocol subgraph for instant historical queries. |
| **Venice model dependency** | Tied to Qwen3-235B via Venice. | Abstract the LLM layer to support model rotation and multi-model consensus scoring. |

## Built With

- [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004) — On-chain agent identity and reputation
- [EAS](https://attest.org) — Ethereum Attestation Service for verifiable trust proofs
- [Venice](https://venice.ai) — Private LLM inference with zero data retention
- [MCP](https://modelcontextprotocol.io) — Model Context Protocol for agent-to-agent tool calls
- [Base](https://base.org) — L2 for on-chain transactions
- [web3.py](https://web3py.readthedocs.io) — Ethereum interaction

## License

MIT
