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
| **Private Inference** | All LLM evaluation runs through [Venice](https://venice.ai)'s policy-based no-data-retention inference. Agent manifests, endpoints, and wallet data are not stored by the LLM provider per Venice's data retention policy (not cryptographically enforced). |
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
| **Manual curation** (allowlists, directories) | Doesn't scale. 20,000+ ERC-8004 agents can't be hand-reviewed. | Fully autonomous by default — discovers, evaluates, and publishes without human intervention. Human review available via `--interactive` flag for opt-in oversight. |
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
| Endpoint Liveness | 20% | Service endpoints responding (200, 401, 403 = alive) |
| On-chain History | 20% | Transaction count, balance, existing reputation, contract code |
| Venice Trust Analysis | 25% | Private risk-categorized LLM evaluation |
| Protocol Declaration | 15% | MCP transport/metadata compliance |

Composite score: 0–100. Bayesian confidence determines auto-publish vs withhold. Weights are env-configurable (`WEIGHT_IDENTITY`, `WEIGHT_LIVENESS`, `WEIGHT_ONCHAIN`, `WEIGHT_VENICE_TRUST`, `WEIGHT_PROTOCOL`).

**Weight Rationale:** Identity (20%) is most gameable. Liveness and on-chain (20% each) require real infrastructure. Venice (25%) is highest because it synthesizes all signals and performs risk-categorized analysis. Protocol compliance (15%) is binary — lowest weight.

See [`SCORING_METHODOLOGY.md`](SCORING_METHODOLOGY.md) for the full mathematical derivation.

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

### Agent-to-Agent Verification

Sentinel exposes trust tools via two transports:

**MCP (stdio)** — for local agent-to-agent verification:

| MCP Tool | Description |
|----------|-------------|
| `verify_agent` | Full trust verification pipeline |
| `check_reputation` | Read reputation from ERC-8004 Reputation Registry |
| `check_validation` | Check Validation Registry status |
| `get_trust_chain` | Score, confidence, timestamp, attestation UID |
| `compute_transitive_trust` | Derived trust when one agent vouches for another |

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

**HTTP API** — for remote access (deployed on Railway):

| HTTP Endpoint | MCP Equivalent |
|---------------|----------------|
| `POST /api/evaluate` | `verify_agent` |
| `GET /api/reputation/{id}` | `check_reputation` |
| `GET /api/trust-chain/{id}` | `get_trust_chain` |
| `POST /api/transitive-trust` | `compute_transitive_trust` |

MCP requires stdio transport (local execution). The HTTP API provides the same functionality for remote agents and the interactive dashboard.

A trust-gated demo client is included — run `python3 dummy_client_agent.py --agent-id 1`.

### Example Output

```
SENTINEL TRUST EVALUATION SUMMARY
=====================================================================================
Agent ID    Score  Conf    I    L    O    V    P  State                     TX
-------------------------------------------------------------------------------------
#42            77    80   80  100   60   70   50  PUBLISHED                 0xabc123...
#43            45    55   60    0   50   40    0  WITHHELD_LOW_CONFIDENCE   N/A
=====================================================================================
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

Terminal: WITHHELD_LOW_CONFIDENCE | FAILED
```

**Auto-publish** when confidence ≥ 70. Below that threshold: `WITHHELD_LOW_CONFIDENCE` (no publish).

## Contracts

| Network | Identity Registry | Reputation Registry | EAS |
|---------|-------------------|---------------------|-----|
| Base Mainnet | [`0x8004...9432`](https://basescan.org/address/0x8004A169FB4a3325136EB29fA0ceB6D2e539a432) | [`0x8004...9B63`](https://basescan.org/address/0x8004BAa17C55a88189AE136b182e5fdA19dE9b63) | [`0x4200...0021`](https://basescan.org/address/0x4200000000000000000000000000000000000021) |
| Base Sepolia | [`0x8004...BD9e`](https://sepolia.basescan.org/address/0x8004A818BFB912233c491871b3d84c89A494BD9e) | [`0x8004...8713`](https://sepolia.basescan.org/address/0x8004B663056A597Dffe9eCcC1965A193B7388713) | [`0x4200...0021`](https://sepolia.basescan.org/address/0x4200000000000000000000000000000000000021) |

| Network | Validation Registry | Status |
|---------|---------------------|--------|
| Base Mainnet | — | Code ready, pending ERC-8004 team deployment |
| Base Sepolia | — | Code ready, pending ERC-8004 team deployment |

## Known Bugs

These are real issues observed in production. Not hypothetical limitations — actual behaviour that doesn't match intent.

**1. Ephemeral results on Railway**
`dashboard/results.json` is written to Railway's ephemeral filesystem. On every redeploy, all evaluation history is wiped from the server. The dashboard re-fetches from `/api/results` which reads this file — so a fresh deploy shows an empty dashboard until evals run again. Workaround: keep a local backup of `results.json` and deploy it with the container, or move to a persistent store.

**2. Validation Registry not deployed**
The Validation Registry integration (`submit_validation_request`, `submit_validation_response`) is fully implemented in `blockchain.py` but the contract hasn't been deployed by the ERC-8004 team to mainnet yet. Challenge mode (`python3 main.py challenge`) will fail silently on mainnet. Works on Sepolia.

**3. IPFS gateway timeouts**
Three IPFS gateways are tried in sequence (dweb.link, ipfs.io, cloudflare-ipfs.com) with tenacity retry. On mainnet, roughly 15–20% of agents declare IPFS manifest URIs that time out across all three gateways. These fall back to identity score 0, which unfairly drags down the composite. No fix without a local IPFS node.

**4. Evaluator wallet ETH depletion**
Each `giveFeedback()` + EAS attestation pair costs ~0.0001–0.0003 ETH in gas on Base Mainnet. With 47 evaluations published, the evaluator wallet has gone from 0.003 ETH down to ~0.002 ETH. At current rate, ~20 more publishes before the wallet runs dry and all evals silently stay in VERIFIED state. Needs topping up before large batch runs.

**5. No rate limiting on `/api/evaluate`**
The evaluate endpoint has a mutex lock (`_eval_lock`) preventing concurrent evals, but no per-IP rate limiting. A single client can queue repeated evals. On Railway's free tier this could exhaust the evaluator wallet quickly if the endpoint is discovered publicly.

**6. Trust score not re-evaluated on manifest changes**
Once an agent is in `results.json` as PUBLISHED, the deduplication logic skips it on future discover passes. If the agent owner updates their manifest URI or changes their endpoints, Sentinel will never re-score them. Stale scores accumulate silently.

---

## Future Plans

Roughly in priority order for what would make Sentinel production-ready.

**Persistent storage**
Replace `results.json` with a proper database (SQLite at minimum, Postgres for scale). Results survive redeploys, queries are indexed, and historical evaluation trends become queryable. This is the single highest-leverage change.

**Scheduled re-evaluation**
Agents change. Endpoints go down, wallets grow, manifests update. A cron job re-evaluating published agents every 30 days and flagging significant score changes would make the trust scores meaningful over time rather than a one-shot snapshot.

**Graph Protocol subgraph for discovery**
Current discovery scans `eth_getLogs` across all blocks — slow and rate-limited on public RPCs. A subgraph over the Identity Registry `Registered` events would make discovery instant and enable filtering (e.g. only agents registered in the last 7 days, only agents by capability domain).

**Multi-model consensus**
Venice runs Qwen3-235B for trust synthesis. A higher-confidence model would run multiple smaller models (or multiple independent Venice calls with different system prompts) and use agreement/disagreement as a confidence signal. Models that agree strongly → high confidence boost. Models that disagree → confidence penalty and human review flag.

**Evaluator staking for sybil resistance**
The current 3-wallet model prevents self-scoring but doesn't prevent an attacker registering 100 agents and colluding with friends to cross-score them. Requiring evaluators to stake ETH — slashed if they're caught in an evaluation ring (detected via HHI graph analysis) — would make collusion economically irrational.

**WebSocket updates for live evaluation**
The dashboard polls `/api/results` on load. During a live eval, the pipeline animation is client-side only — there's no server push when the eval completes. A WebSocket endpoint streaming state transitions (`FETCH_IDENTITY`, `CHECK_LIVENESS`, etc.) would make the pipeline animation reflect real progress rather than a fixed timer.

**TLS and semantic endpoint verification**
Current liveness check: HTTP HEAD, interpret status code. Future: verify TLS certificate validity, check certificate chain, probe the endpoint with a minimal MCP initialize call and validate the response schema. A server returning 200 to any request is not the same as a real AI agent endpoint.

**ENS reverse resolution for all agents**
Currently ENS lookup is attempted per wallet but skipped if it times out. A background ENS resolution pass over all evaluated agents (using a local ENS resolver or The Graph) would enrich identity scores and surface agents with committed on-chain identities.

**Agent capability graph**
Sentinel currently evaluates agents in isolation. The transitive trust computation (`compute_transitive_trust`) is implemented but not visualised. A directed graph of "agent A trusts agent B" relationships — rendered in the dashboard — would make the trust network topology visible and help identify high-centrality agents worth evaluating first.

## Development History

Sentinel was developed during the [Synthesis Hackathon](https://synthesis.md/) (March 2026) by Gayathri Satheesh with Claude Code as the AI engineering assistant. The project evolved from an earlier prototype called AQE (Agent Quality Evaluator) that targeted smart contract evaluation — the core pipeline architecture (discover → verify → analyze → score → publish) was adapted for agent trust evaluation when the ERC-8004 opportunity became clear.

The full development conversation log is available in [`CONVERSATION_LOG.md`](CONVERSATION_LOG.md).

## Built With

- [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004) — On-chain agent identity and reputation
- [EAS](https://attest.org) — Ethereum Attestation Service for verifiable trust proofs
- [Venice](https://venice.ai) — Private LLM inference with zero data retention
- [MCP](https://modelcontextprotocol.io) — Model Context Protocol for agent-to-agent tool calls
- [Base](https://base.org) — L2 for on-chain transactions
- [web3.py](https://web3py.readthedocs.io) — Ethereum interaction

## License

MIT
