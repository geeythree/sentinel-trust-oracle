# Sentinel — Human-Agent Collaboration Log

**Human**: Gayathri Satheesh
**Agent**: Claude Code (claude-opus-4-6)
**Harness**: Claude Code CLI
**Period**: March 8–22, 2026
**Sessions**: 16 conversations, ~50 MB of raw transcript

This is a curated highlights document covering the key decisions, pivots, and breakthroughs during Sentinel's development. The raw transcripts live in Claude Code's local session storage.

---

## Phase 1: Problem Discovery (Mar 8–9)

### Identifying the gap

**Human**: Explored the ERC-8004 spec and the Synthesis hackathon bounties. Key observation: the Identity Registry on Base had 20,000+ agents registered, but the Reputation Registry was completely empty. No product was actually verifying agents or populating trust data.

**Agent**: Researched the ERC-8004 contract ABIs, discovered the three-registry model (Identity, Reputation, Validation), and confirmed that `giveFeedback()` on the Reputation Registry had zero calls. Cross-referenced with MCP CVE reports (CVSS 9.6 RCE from tool poisoning attacks) to build the threat model.

**Decision**: Build an autonomous trust oracle that fills the empty Reputation Registry — targeting all three hackathon bounties (Protocol Labs "Let the Agent Cook", PL_Genesis "Agents With Receipts", Venice "Private Agents").

### Architecture brainstorm

**Human**: Wanted a pipeline approach — sequential stages that each produce a score, then composite them.

**Agent**: Proposed 4 trust dimensions: Identity Completeness, Endpoint Liveness, On-chain History, and Venice Trust Analysis. We debated weights extensively.

**Pivot**: Initially all dimensions were equally weighted (25% each). Human pushed to give Venice 30% because "the LLM is the only component that can interpret ambiguous signals." Agent agreed after testing showed mechanical heuristics couldn't distinguish legitimate new agents from dormant scam wallets. Final weights: 20% identity, 25% liveness, 25% on-chain, 30% Venice.

---

## Phase 2: Initial Implementation (Mar 14–15)

### 3,371-line first commit

**Agent**: Built the entire initial codebase in one session — 27 files including the state machine orchestrator, all 4 evaluation modules, blockchain integration (web3.py + EAS), Venice client, MCP server, CLI with 6 modes, and a basic dashboard.

**Human**: Reviewed the architecture, tested locally, pushed to GitHub.

### Key design decisions made together

1. **3-wallet model**: Agent suggested separating OPERATOR (owns identity) from EVALUATOR (writes reputation) to prevent self-scoring attacks. Human added AUDITOR as optional third wallet for self-reputation experiments.

2. **State machine**: We chose a 13-state FSM over a simpler pipeline because the agent needed intermediate states for human review (PENDING_HUMAN_REVIEW) and confidence-based routing (WITHHELD_LOW_CONFIDENCE vs auto-PUBLISH).

3. **Tool call budget**: Capped at 15 calls per evaluation. Agent tracks budget via the logger — `consume_budget()` before each external call, `BudgetExhaustedError` at limit. This was a direct response to the "Let the Agent Cook" bounty requiring safety guardrails.

4. **401/403 = good**: Agent initially marked 401/403 HTTP responses as failures. Human pointed out that auth-protected endpoints are actually a positive signal — they mean the service exists and is secured. Changed to: 200 = live, 401/403 = secured (full score), 5xx/timeout = dead.

---

## Phase 3: Venice Integration (Mar 14–15)

### The 4-layer parse fallback — a breakthrough

**Problem**: Venice's `response_format` with `strict: true` wasn't supported at the API level, causing JSON parse failures on ~30% of evaluations.

**Agent**: Proposed a 4-layer fallback strategy:
1. JSON schema validation via `response_format`
2. Regex extraction from markdown fences (```json blocks)
3. Retry with a correction prompt ("Your previous response was not valid JSON...")
4. Fallback to neutral score (50) with `venice_parse_failed=True`

**Human**: Tested each layer independently. Layer 1 (json_schema) succeeded ~70% of the time. Layer 2 (regex) caught another ~25%. Layer 3 (retry) caught ~4%. Layer 4 (neutral fallback) handled the remaining ~1%.

**Result**: 8/8 evaluations in production used json_schema successfully. The fallback exists but hasn't been needed on the current model (qwen3-235b).

### Privacy-first design

**Human**: "I don't want Sentinel to become a surveillance tool. Agent data should stay private."

**Agent**: Designed the architecture so that only the final composite score, confidence, and dimension breakdown go on-chain via EAS attestations. The raw evaluation inputs (manifest contents, endpoint responses, wallet history, Venice reasoning) are never published. Venice's zero-data-retention policy means even the LLM provider doesn't store the evaluation.

**Breakthrough**: This became the core differentiator — "privacy-preserving trust scoring." The README section "Why Venice Is Essential (Not Optional)" was written to explain this to judges.

---

## Phase 4: Production Hardening (Mar 18)

### 38 audit issues in one session

**Agent**: Ran a systematic audit of all 20 source files and identified 38 issues across 9 categories: error handling, gas estimation, timeout handling, crash recovery, SSRF protection, input validation, blockchain retry logic, test coverage, and logging.

**Human**: Prioritized: "Fix the ones that could cause real failures first."

**Agent**: Fixed all 38 in a single commit (1,167 insertions, 328 deletions across 20 files). Key fixes:
- Gas estimation with fallback (was hardcoded, now dynamic)
- SSRF protection blocking localhost/private IPs in liveness checker
- Tenacity retry on all web3 calls (3 attempts, exponential backoff)
- Crash recovery in watch mode (evaluation failures don't kill the loop)
- 138 unit tests added (from 0)

### Self-registration on Base

**Human**: "Sentinel should eat its own dogfood — register itself as an ERC-8004 agent."

**Agent**: Wrote the registration flow, and Sentinel registered on-chain as **Agent #33465** on Base. The `agent.json` manifest was updated with the assigned ID and pushed to the repo.

---

## Phase 5: On-chain Evaluations (Mar 18)

### First published evaluations

Ran `python3 main.py discover --testnet --max-agents 5` and evaluated 8 real agents from the Base Sepolia Identity Registry.

**Results**:
| Agent | Score | State | What Venice Found |
|-------|-------|-------|------------------|
| #5 | 72 | PUBLISHED | "Well-structured Olas marketplace manifest with complete fields" |
| #10 | 72 | PUBLISHED | "Complete Olas marketplace manifest with all required fields" |
| #50 | 70 | PUBLISHED | "Full manifest. Endpoint responsive. However zero on-chain history" |
| #100 | 71 | PUBLISHED | "Olas marketplace agent with complete manifest and responsive endpoints" |
| #1 | 50 | WITHHELD | "Partial manifest with endpoint declarations but no liveness" |
| #500 | 19 | WITHHELD | "Empty agent URI — no manifest available" |
| #2093 | 20 | WITHHELD | "URI points to 8004scan.io which returned non-manifest content" |
| #2094 | 86 | VERIFIED | "LAIDA agent with well-defined manifest including cryptographic proof capabilities" |

**Breakthrough**: The scoring system correctly differentiated between legitimate agents (Olas marketplace agents with good manifests) and problematic ones (empty URIs, broken manifests, no endpoints). The confidence-based routing worked: high-confidence evaluations auto-published, low-confidence ones were withheld.

Each published evaluation produced a real transaction hash and EAS attestation UID — verifiable on BaseScan and EASScan.

---

## Phase 6: Dashboard & Demo Polish (Mar 19–20)

### Surfacing proof-of-work

**Human**: "Judges won't dig through JSON files. Everything needs to be visible on the dashboard."

**Agent**: Added Venice stats banner (API calls, tokens, model, parse success), Autonomy stats banner (pipeline stages, tool budget, published/withheld counts), and ERC-8004 multi-registry banner (identity/reputation/EAS contract addresses) to the dashboard.

### Cyberpunk redesign

**Human**: "The white dashboard looks bland. Make it dark cyberpunk for the demo."

**Agent**: Complete visual redesign — dark glassmorphism theme with:
- Animated CSS grid background
- Neon cyan/violet/emerald accent colors
- Animated pipeline visualization during live evaluations
- Score counter tick-up animations
- Radar chart SVG glow effects
- Pulsing badges for Published/Verified states

### Production-readiness pass

**Agent**: Final audit identified 7 issues: XSS vulnerabilities in `renderEvaluation()`, missing `rel="noopener noreferrer"` on external links, no null safety on API data, duplicate SVG filter IDs, crushed mobile layouts on 5-column grids, missing meta tags, and no favicon. All fixed in one pass.

---

## Phase 7: Mainnet & Final Hardening (Mar 22)

### SSRF IP-pinning bug

**Agent**: Production audit found that the SSRF guard in `agent_verifier.py` was replacing hostnames with raw IPs in HTTPS URLs for DNS rebinding protection. This broke TLS certificate verification on all CDN-hosted manifests (GitHub, Cloudflare, etc.) — SSL cert is issued for `raw.githubusercontent.com`, not `185.199.110.133`.

**Human**: Pushed for mainnet evaluation of Sentinel itself as proof that the system works end-to-end on production infrastructure.

**Fix**: HTTPS connections are already protected against DNS rebinding by TLS certificate validation, so IP pinning is only needed for HTTP. Changed to skip pinning on HTTPS, keep it for HTTP.

### Mainnet self-evaluation — Agent #33465

After fixing the SSRF bug, Sentinel evaluated itself on Base Mainnet:

| Dimension | Score |
|-----------|-------|
| Identity | 90 |
| Liveness | 50 (stdio:// endpoint = neutral) |
| On-chain | 55 |
| Venice Trust | 78 |
| **Composite** | **68** |
| **Confidence** | **85%** |
| **State** | **PUBLISHED** |

**TX**: [`0x7d5a7d73...`](https://basescan.org/tx/7d5a7d73109f71c2a5be961e50a0b8f7147ce739626fa2cd8815544eedc46e97) — a real mainnet transaction writing Sentinel's trust score to the ERC-8004 Reputation Registry.

**Breakthrough**: Sentinel evaluating itself demonstrates circular trust — a trust oracle that practices what it preaches. The published score is verifiable by anyone on BaseScan.

---

## Key Pivots & Decisions Summary

| Decision | What we considered | What we chose | Why |
|----------|-------------------|---------------|-----|
| Project name | AQE (Agent Quality Evaluator) | Sentinel | Better reflects the trust oracle role |
| Trust dimensions | 3 vs 4 vs 5 dimensions | 4 | Identity, Liveness, On-chain, Venice cover all signal types without overlap |
| Venice weight | 25% (equal) vs 30% (boosted) | 30% | Venice is the interpretive layer; raw metrics alone can't distinguish intent |
| Confidence routing | Threshold at 50% vs 70% | 70% | Conservative — only publish when confident to avoid polluting the registry |
| 401/403 handling | Failure vs success | Secured = success | Auth-protected endpoints prove the service exists |
| Wallet scoring | Penalize new wallets vs neutral | Neutral (50) | New agents shouldn't be punished for being new |
| Parse fallback | Fail hard vs graceful degradation | 4-layer fallback | Venice JSON parsing isn't deterministic; need resilience |
| Self-registration | Optional vs required | Required | "Eat your own dogfood" — Sentinel is Agent #33465 |

---

## Human-Agent Collaboration Breakdown

### Decisions the Human Drove

These are architectural choices that shaped the product — not cosmetic preferences:

| Decision | Human's Reasoning | Impact |
|----------|------------------|--------|
| **401/403 = secured = full score** | "Auth-protected endpoints prove the service exists and is properly secured. Marking them as failures is wrong." | Changed endpoint liveness from a naive HTTP-200-only check to a security-aware interpretation. Without this, every gated API would score 0 on liveness. |
| **Neutral scoring for new wallets** | "New agents shouldn't be punished for being new — that creates a chicken-and-egg problem where no new agent can ever score well." | On-chain baseline set to 50 (neutral) instead of 0. Prevents the registry from becoming a walled garden for established agents. |
| **Confidence threshold at 70%, not 50%** | "Only publish when we're confident. Wrong scores on-chain are worse than no scores." | Conservative routing ensures the Reputation Registry isn't polluted with low-confidence evaluations. |
| **Venice weight at 30%, not 25%** | "The LLM is the only component that can interpret ambiguous signals. Raw metrics alone can't distinguish intent." | Venice became the interpretive layer — the component that turns numbers into judgment. |
| **3-wallet separation** | "The evaluator wallet must differ from the operator wallet to prevent self-scoring attacks." Added AUDITOR as optional third wallet for self-reputation experiments. | Prevents Sentinel from gaming its own reputation score. |
| **Privacy-first mandate** | "I don't want Sentinel to become a surveillance tool. Agent data should stay private." | Drove the decision to use Venice's zero-data-retention inference. Only composite scores go on-chain — raw evaluation data never leaves the local process. |
| **"Eat your own dogfood"** | "Sentinel should register as an ERC-8004 agent and evaluate itself." | Sentinel became Agent #33465, demonstrating circular trust. |

### What the Agent Built

| Area | Contribution |
|------|-------------|
| Architecture | State machine implementation, pipeline coordinator, confidence routing |
| Code | 4,000+ lines across 27 files — all modules, CLI, MCP server |
| Blockchain | web3.py integration, ERC-8004 read/write, EAS attestations, gas estimation |
| Venice | Full client with 4-layer parse fallback, prompt engineering |
| Security | SSRF protection, XSS prevention, input validation, budget enforcement |
| Testing | 138 unit tests, production hardening audit (38 issues fixed in one session) |
| Dashboard | Cyberpunk redesign with animations, live evaluation pipeline |

### How We Worked Together

The human identified what was *wrong* with the naive approach (new wallets penalized, secured endpoints marked dead, low-confidence scores published). The agent translated those insights into code. When the agent proposed 4 trust dimensions, the human tested them against real agents and pushed back on the weighting. When the agent built the liveness checker, the human caught the 401/403 misinterpretation. When the agent suggested equal weights, the human argued for Venice's interpretive role.

This wasn't "human has vision, agent writes code." It was iterative disagreement and refinement — the human challenged mechanical defaults, and the agent implemented the nuanced alternatives.
