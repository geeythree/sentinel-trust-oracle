"""MCP server exposing Sentinel as verify_agent and check_reputation tools."""
from __future__ import annotations

import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


app = Server("sentinel-trust-oracle")


def _get_pipeline():
    """Lazy-initialize the full Sentinel pipeline."""
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

    return orchestrator, discovery, blockchain


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="verify_agent",
            description=(
                "Verify an ERC-8004 registered agent's identity, liveness, and "
                "trustworthiness. Returns a trust verdict with on-chain proof."
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
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    orchestrator, discovery, blockchain = _get_pipeline()

    if name == "verify_agent":
        agent_id = arguments["agent_id"]
        agent = discovery.discover_agent_by_id(agent_id)
        result = orchestrator.evaluate_single(agent)
        return [TextContent(type="text", text=json.dumps(result.to_verdict_dict()))]

    elif name == "check_reputation":
        agent_id = arguments["agent_id"]
        try:
            count, value, decimals = blockchain.get_reputation_summary(agent_id)
            return [TextContent(type="text", text=json.dumps({
                "agent_id": agent_id,
                "feedback_count": count,
                "summary_value": value,
                "summary_decimals": decimals,
            }))]
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({
                "agent_id": agent_id,
                "error": str(e),
            }))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


async def run_mcp_server():
    """Entry point for MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
