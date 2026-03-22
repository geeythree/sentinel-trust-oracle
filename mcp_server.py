"""MCP server exposing Sentinel as verify_agent and check_reputation tools."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

_log = logging.getLogger(__name__)


app = Server("sentinel-trust-oracle")

# Module-level singleton cache for the pipeline
_pipeline_cache: tuple | None = None


def _get_pipeline():
    """Lazy-initialize the full Sentinel pipeline (cached after first call)."""
    global _pipeline_cache
    if _pipeline_cache is not None:
        return _pipeline_cache

    import config as config_module
    if config_module.config is None:
        config_module.config = config_module.create_config()
    from config import config

    from logger import AgentLogger
    from agent_discovery import AgentDiscovery
    from agent_verifier import AgentVerifier
    from liveness_checker import LivenessChecker
    from onchain_analyzer import OnchainAnalyzer
    from venice import VeniceClient
    from scorer import Scorer
    from blockchain import BlockchainClient
    from orchestrator import Orchestrator

    logger = AgentLogger(config.AGENT_LOG_PATH, budget=config.TOOL_CALL_BUDGET)
    blockchain = BlockchainClient(logger)
    discovery = AgentDiscovery(logger, blockchain)
    agent_verifier = AgentVerifier(logger)
    liveness_checker = LivenessChecker(logger)
    onchain_analyzer = OnchainAnalyzer(logger, blockchain)
    venice = VeniceClient(logger)
    scorer = Scorer()

    orchestrator = Orchestrator(
        logger, discovery, agent_verifier, liveness_checker,
        onchain_analyzer, venice, scorer, blockchain,
    )

    _pipeline_cache = (orchestrator, discovery, blockchain)
    return _pipeline_cache


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="verify_agent",
            description=(
                "Verify an ERC-8004 registered agent's identity, liveness, and "
                "trustworthiness. Returns a trust verdict with onchain proof."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "integer",
                        "description": "ERC-8004 agent ID (tokenId)",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="check_reputation",
            description=(
                "Check an agent's existing reputation score from the "
                "ERC-8004 Reputation Registry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "integer",
                        "description": "ERC-8004 agent ID",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="check_validation",
            description=(
                "Check validation status for an agent from the "
                "ERC-8004 Validation Registry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "integer",
                        "description": "ERC-8004 agent ID",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="get_trust_chain",
            description=(
                "Get an agent's trust chain: score, confidence, evaluation timestamp, "
                "and EAS attestation UID for independent on-chain verification."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "integer",
                        "description": "ERC-8004 agent ID",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="compute_transitive_trust",
            description=(
                "Compute transitive trust: if a requesting agent (with known score/confidence) "
                "vouches for a target agent, returns the derived trust score. "
                "Derived trust = requester_confidence × target_score / 100."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "requester_agent_id": {
                        "type": "integer",
                        "description": "Agent ID of the requester (vouching agent)",
                    },
                    "target_agent_id": {
                        "type": "integer",
                        "description": "Agent ID of the target being vouched for",
                    },
                },
                "required": ["requester_agent_id", "target_agent_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    orchestrator, discovery, blockchain = _get_pipeline()
    loop = asyncio.get_running_loop()

    if name == "verify_agent":
        agent_id = arguments.get("agent_id")
        if agent_id is None or not isinstance(agent_id, int) or agent_id < 0:
            return [TextContent(type="text", text=json.dumps({"error": "agent_id must be a non-negative integer"}))]
        try:
            agent = await loop.run_in_executor(None, discovery.discover_agent_by_id, agent_id)
            result = await loop.run_in_executor(None, orchestrator.evaluate_single, agent)
            return [TextContent(type="text", text=json.dumps(result.to_verdict_dict()))]
        except Exception as e:
            _log.exception("verify_agent failed for agent %s", agent_id)
            return [TextContent(type="text", text=json.dumps({
                "agent_id": agent_id,
                "error": "Evaluation failed. Please try again.",
            }))]

    elif name == "check_reputation":
        agent_id = arguments.get("agent_id")
        if agent_id is None or not isinstance(agent_id, int) or agent_id < 0:
            return [TextContent(type="text", text=json.dumps({"error": "agent_id must be a non-negative integer"}))]
        try:
            count, value, decimals, hhi, unique = await loop.run_in_executor(
                None, blockchain.get_reputation_with_hhi, agent_id
            )
            return [TextContent(type="text", text=json.dumps({
                "agent_id": agent_id,
                "feedback_count": count,
                "summary_value": value,
                "summary_decimals": decimals,
                "unique_reviewer_count": unique,
                "hhi": hhi,
                "concentration": (
                    "HIGH_CONCENTRATION" if hhi > 2500 else
                    "MODERATE_CONCENTRATION" if hhi > 1500 else
                    "HEALTHY"
                ),
            }))]
        except Exception as e:
            _log.exception("check_reputation failed for agent %s", agent_id)
            return [TextContent(type="text", text=json.dumps({
                "agent_id": agent_id,
                "error": "Reputation lookup failed. Please try again.",
            }))]

    elif name == "check_validation":
        agent_id = arguments.get("agent_id")
        if agent_id is None or not isinstance(agent_id, int) or agent_id < 0:
            return [TextContent(type="text", text=json.dumps({"error": "agent_id must be a non-negative integer"}))]
        if not blockchain.has_validation_registry:
            return [TextContent(type="text", text=json.dumps({
                "agent_id": agent_id,
                "status": "Validation Registry not deployed",
                "validation_count": 0,
            }))]
        try:
            count, avg = await loop.run_in_executor(
                None, blockchain.get_validation_summary, agent_id
            )
            return [TextContent(type="text", text=json.dumps({
                "agent_id": agent_id,
                "validation_count": count,
                "avg_response": avg,
            }))]
        except Exception as e:
            _log.exception("check_validation failed for agent %s", agent_id)
            return [TextContent(type="text", text=json.dumps({
                "agent_id": agent_id,
                "error": "Validation lookup failed. Please try again.",
            }))]

    elif name == "get_trust_chain":
        agent_id = arguments.get("agent_id")
        if agent_id is None or not isinstance(agent_id, int) or agent_id < 0:
            return [TextContent(type="text", text=json.dumps({"error": "agent_id must be a non-negative integer"}))]
        try:
            # Read from cached results
            from config import config
            import os
            results_path = config.DASHBOARD_RESULTS_PATH
            if results_path.exists():
                with open(results_path) as f:
                    results = json.load(f)
                for r in results:
                    if r.get("agent_id") == agent_id:
                        return [TextContent(type="text", text=json.dumps({
                            "agent_id": agent_id,
                            "trust_score": r.get("composite_score"),
                            "confidence": r.get("confidence"),
                            "timestamp": r.get("timestamp"),
                            "attestation_uid": r.get("attestation_uid"),
                            "tx_hash": r.get("tx_hash"),
                            "dimensions": r.get("dimensions"),
                            "input_hash": r.get("input_hash"),
                            "chain_id": r.get("chain_id"),
                            "state": r.get("state"),
                        }))]
            # Not found in cache — try live evaluation
            agent = await loop.run_in_executor(None, discovery.discover_agent_by_id, agent_id)
            result = await loop.run_in_executor(None, orchestrator.evaluate_single, agent)
            return [TextContent(type="text", text=json.dumps({
                "agent_id": agent_id,
                "trust_score": result.composite_score,
                "confidence": result.evaluation_confidence,
                "timestamp": result.timestamp,
                "attestation_uid": result.attestation_uid,
                "tx_hash": result.tx_hash,
                "dimensions": {
                    "identity_completeness": result.dimensions.identity_completeness,
                    "endpoint_liveness": result.dimensions.endpoint_liveness,
                    "onchain_history": result.dimensions.onchain_history,
                    "venice_trust_analysis": result.dimensions.venice_trust_analysis,
                },
                "input_hash": result.input_hash,
                "state": result.state.value,
            }))]
        except Exception as e:
            _log.exception("get_trust_chain failed for agent %s", agent_id)
            return [TextContent(type="text", text=json.dumps({"agent_id": agent_id, "error": "Trust chain lookup failed."}))]

    elif name == "compute_transitive_trust":
        req_id = arguments.get("requester_agent_id")
        tgt_id = arguments.get("target_agent_id")
        if not all(isinstance(x, int) and x >= 0 for x in [req_id, tgt_id] if x is not None):
            return [TextContent(type="text", text=json.dumps({"error": "Both agent IDs must be non-negative integers"}))]
        try:
            from config import config
            results_path = config.DASHBOARD_RESULTS_PATH
            req_data = tgt_data = None
            if results_path.exists():
                with open(results_path) as f:
                    results = json.load(f)
                for r in results:
                    if r.get("agent_id") == req_id:
                        req_data = r
                    if r.get("agent_id") == tgt_id:
                        tgt_data = r

            if req_data is None:
                agent = await loop.run_in_executor(None, discovery.discover_agent_by_id, req_id)
                result = await loop.run_in_executor(None, orchestrator.evaluate_single, agent)
                req_data = {"composite_score": result.composite_score, "confidence": result.evaluation_confidence}
            if tgt_data is None:
                agent = await loop.run_in_executor(None, discovery.discover_agent_by_id, tgt_id)
                result = await loop.run_in_executor(None, orchestrator.evaluate_single, agent)
                tgt_data = {"composite_score": result.composite_score, "confidence": result.evaluation_confidence}

            # Subjective Logic discount operator (Jøsang 2016)
            # Represent each agent's score/confidence as a Subjective Logic opinion:
            #   belief b = score/100, uncertainty u = 1 - confidence/100
            #   disbelief d = 1 - b - u
            # Discount: A trusts B (requester), B trusts X (target)
            #   b_derived = b_A * b_B
            #   d_derived = b_A * d_B
            #   u_derived = d_A + u_A + b_A * u_B
            # Projected probability P = b + a*u (base rate a=0.5)
            req_score = req_data.get("composite_score", 0) / 100.0
            req_uncertainty = 1.0 - req_data.get("confidence", 0) / 100.0
            req_disbelief = max(0.0, 1.0 - req_score - req_uncertainty)

            tgt_score = tgt_data.get("composite_score", 0) / 100.0
            tgt_uncertainty = 1.0 - tgt_data.get("confidence", 0) / 100.0
            tgt_disbelief = max(0.0, 1.0 - tgt_score - tgt_uncertainty)

            b_derived = req_score * tgt_score
            d_derived = req_score * tgt_disbelief
            u_derived = req_disbelief + req_uncertainty + req_score * tgt_uncertainty
            # Clamp to valid opinion (b+d+u=1)
            total = b_derived + d_derived + u_derived
            if total > 0:
                b_derived /= total
                d_derived /= total
                u_derived /= total

            # Projected probability with base rate 0.5
            projected = b_derived + 0.5 * u_derived
            derived_trust = round(projected * 100, 1)
            derived_confidence = round((1.0 - u_derived) * 100, 1)

            return [TextContent(type="text", text=json.dumps({
                "requester_agent_id": req_id,
                "requester_score": req_data.get("composite_score"),
                "requester_confidence": req_data.get("confidence"),
                "target_agent_id": tgt_id,
                "target_score": tgt_data.get("composite_score"),
                "target_confidence": tgt_data.get("confidence"),
                "derived_trust": derived_trust,
                "derived_confidence": derived_confidence,
                "model": "subjective_logic_discount",
                "interpretation": (
                    "TRUSTED" if derived_trust >= 70 else
                    "MODERATE" if derived_trust >= 50 else
                    "LOW_TRUST"
                ),
                "note": (
                    f"Trust decays through the referral chain. "
                    f"Uncertainty u={u_derived:.3f} grows with each hop — "
                    f"long chains converge to base rate 50."
                ),
            }))]
        except Exception as e:
            _log.exception("compute_transitive_trust failed")
            return [TextContent(type="text", text=json.dumps({"error": "Transitive trust computation failed."}))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


async def run_mcp_server():
    """Entry point for MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_mcp_server())
