# Sentinel — Autonomous Agent Trust Oracle

Autonomous agent that verifies ERC-8004 agent identities, checks endpoint liveness, analyzes on-chain history, and writes verifiable trust scores to the Reputation Registry via EAS attestations on Base.

Built for the **Synthesis hackathon** (March 4-25, 2026). Refactored from AQE (Agent Quality Evaluator) — same pipeline architecture, different evaluation target (agents instead of contracts).

## Hackathon Targets
- **PL_Genesis** "Agents With Receipts — ERC-8004" ($4,000 1st) — multi-registry ERC-8004
- **Protocol Labs** "Let the Agent Cook" ($4,000 1st) — fully autonomous agent
- **Venice** "Private Agents, Trusted Actions" ($5,750 1st) — private cognition via Venice

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in: OPERATOR_PRIVATE_KEY, EVALUATOR_PRIVATE_KEY, VENICE_API_KEY
```

### 3-Wallet Model (REQUIRED — keys must be different wallets)
- **OPERATOR** — Owns Sentinel's ERC-8004 identity, registers agent on Identity Registry
- **EVALUATOR** — Submits `giveFeedback()` to Reputation Registry (must be different wallet)
- **AUDITOR** — Optional, for self-reputation

Generate keys: `python3 -c "from eth_account import Account; a = Account.create(); print(a.key.hex())"`

### One-Time Setup Steps
1. `python3 main.py register-schema` → get EAS_SCHEMA_UID, add to .env
2. `python3 main.py register --agent-uri <url-to-agent.json>` → registers ERC-8004 identity

## Running

```bash
# Discover and evaluate agents on Base Sepolia
python3 main.py discover --testnet --max-agents 5

# Evaluate a specific agent by ID
python3 main.py manual --agent-id 1

# Evaluate agent by owner address
python3 main.py manual --address 0x...

# Mainnet
python3 main.py discover --mainnet

# Start MCP server (for agent-to-agent verification)
python3 main.py mcp-server

# Demo: client agent calls Sentinel via MCP
python3 dummy_client_agent.py --agent-id 1

# EAS validation challenge
python3 main.py challenge --evaluation-id <id> --reason "Score seems inflated"
```

## Architecture

### Files
| File | Purpose |
|------|---------|
| `main.py` | CLI entry point — 6 modes: discover, manual, register, register-schema, challenge, mcp-server |
| `orchestrator.py` | Pipeline coordinator with state machine |
| `agent_discovery.py` | ERC-8004 Identity Registry event scanning |
| `agent_verifier.py` | Fetch agent.json, IPFS resolution, validate manifest, score identity |
| `liveness_checker.py` | HTTP endpoint liveness checking (401/403 = secured = good) |
| `onchain_analyzer.py` | Wallet history analysis, existing reputation check |
| `venice.py` | Venice API (Qwen3-235B) — private trust analysis |
| `scorer.py` | Weighted composite: 20% identity, 25% liveness, 25% on-chain, 30% Venice |
| `blockchain.py` | Web3.py — ERC-8004 Identity/Reputation + EAS attestations |
| `logger.py` | Append-mode JSON Lines logger (`agent_log.json`) |
| `config.py` | Deferred config creation from env vars (fixes --mainnet bug) |
| `models.py` | All dataclasses and enums — single source of truth |
| `exceptions.py` | Exception hierarchy rooted at `SentinelError` |
| `mcp_server.py` | MCP server exposing verify_agent and check_reputation tools |
| `dummy_client_agent.py` | Demo script: client agent calls Sentinel via MCP |
| `agent.json` | Sentinel's own agent manifest for ERC-8004 registration |

### Pipeline (sequential)
```
DISCOVERED → PLANNING → FETCH_IDENTITY → CHECK_LIVENESS → ON-CHAIN_ANALYSIS
  → VENICE_TRUST → SCORING → VERIFYING → PUBLISHING → PUBLISHED
```
Terminal states: `WITHHELD_LOW_CONFIDENCE`, `PENDING_HUMAN_REVIEW`, `FAILED`

### Trust Dimensions (4)
| Dimension | Weight | Source |
|-----------|--------|--------|
| Identity Completeness | 20% | agent_verifier.py — manifest fields, services |
| Endpoint Liveness | 25% | liveness_checker.py — HTTP status codes |
| On-chain History | 25% | onchain_analyzer.py — tx count, balance, reputation |
| Venice Trust Analysis | 30% | venice.py — private LLM evaluation |

### State Determination Logic
- confidence >= 70 → `VERIFIED` (auto-publish to chain)
- confidence < 70 AND spread > 50 → `PENDING_HUMAN_REVIEW`
- confidence < 70 AND spread <= 50 → `WITHHELD_LOW_CONFIDENCE`

### Confidence Scoring
- Identity verified (complete manifest): +25 (partial: +15)
- Liveness check (all endpoints respond): +25 (some dead: +15)
- Venice clean JSON parse: +25 (regex fallback: +15, parse fail: +0)
- On-chain data (has history): +25 (no history: +15)
- Score spread > 50: -30 penalty
- Score spread > 30: -15 penalty

### Venice LLM — 4-Layer Parse Fallback
1. JSON schema validation (`response_format`)
2. Regex extraction from markdown fences
3. Retry with correction prompt
4. Fallback: neutral score (50) with `venice_parse_failed=True`

## MCP Integration

Sentinel exposes two MCP tools via stdio transport:
- `verify_agent(agent_id)` — full trust verification pipeline
- `check_reputation(agent_id)` — read existing reputation from ERC-8004

Other agents can call these tools to verify agents before interacting with them.

## Contracts & Networks

| Network | Identity Registry | Reputation Registry | EAS |
|---------|-------------------|---------------------|-----|
| Base Mainnet (8453) | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` | `0x4200...0021` |
| Base Sepolia (84532) | `0x8004A818BFB912233c491871b3d84c89A494BD9e` | `0x8004B663056A597Dffe9eCcC1965A193B7388713` | `0x4200...0021` |

Default: testnet (`USE_TESTNET=true`).

## Shell Conventions
- Always use `python3` (not `python`) when running Python commands in the terminal

## Coding Conventions
- All data structures in `models.py` — nowhere else
- All config in `config.py` via env vars — no hardcoded values in modules
- Config is created via `create_config()` AFTER CLI args are parsed (fixes --mainnet bug)
- Dependency injection: orchestrator receives all modules in constructor
- Logger uses append-mode JSON Lines (one object per line), flushed per entry
- `timed_action()` context manager for auto-logged + auto-timed operations
- Budget tracked by logger: `consume_budget()` before each tool call, `BudgetExhaustedError` at 15 calls
- Web3 retry logic via `tenacity` (3 attempts, exponential backoff)
- No penalty for new wallets in on-chain scoring (neutral baseline = 50)
- 401/403 HTTP status = secured endpoint = full score (not a failure)
- Human review recursion capped at 2 retries to prevent infinite loops

## Known Limitations
- Tool call budget is 15 per evaluation (identity=1, liveness=1, onchain=1, venice=1, publish=1 = 5 minimum)
- IPFS gateways frequently timeout — 3 gateway fallback with tenacity retry
- Venice `strict: true` NOT supported at `response_format` level — hence the 4-layer fallback
- Human review timeout: 300s, defaults to DISCARD
- Non-HTTP endpoints (stdio://) get neutral score (50) in liveness check
