"""CLI entry point for Sentinel — Autonomous Agent Trust Oracle."""
from __future__ import annotations

import argparse
import os
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sentinel — Autonomous Agent Trust Oracle",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # Discovery mode
    discover = subparsers.add_parser("discover", help="Scan ERC-8004 for new agents, evaluate, publish")
    discover.add_argument("--start-block", type=int, default=None, help="Start block for scan")
    discover.add_argument("--end-block", type=int, default=None, help="End block for scan")
    discover.add_argument("--max-agents", type=int, default=10, help="Max agents to evaluate")

    # Manual mode
    manual = subparsers.add_parser("manual", help="Evaluate specific agent by ID or address")
    manual.add_argument("--agent-id", type=int, help="ERC-8004 agent ID (tokenId)")
    manual.add_argument("--address", type=str, help="Agent owner wallet address")

    # Register mode (one-time setup)
    register = subparsers.add_parser("register", help="Register Sentinel's ERC-8004 identity")
    register.add_argument("--agent-uri", type=str, required=True, help="URI to agent.json")

    # Register EAS schema (one-time)
    subparsers.add_parser("register-schema", help="Register EAS trust verdict schema")

    # Challenge mode (EAS validation)
    challenge = subparsers.add_parser("challenge", help="Challenge an existing trust verdict")
    challenge.add_argument("--evaluation-id", type=str, required=True)
    challenge.add_argument("--reason", type=str, required=True)

    # MCP server mode
    subparsers.add_parser("mcp-server", help="Start MCP server (stdio transport)")

    # Common arguments
    parser.add_argument("--testnet", action="store_true", default=True, help="Use Base Sepolia (default)")
    parser.add_argument("--mainnet", action="store_true", help="Use Base Mainnet")

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # FIX: Set env vars BEFORE creating config
    if args.mainnet:
        os.environ["USE_TESTNET"] = "false"

    # Deferred config creation (after env vars are set)
    import config as config_module
    config_module.config = config_module.create_config()
    from config import config

    # MCP server mode doesn't need full validation
    if args.mode == "mcp-server":
        from mcp_server import run_mcp_server
        import asyncio
        asyncio.run(run_mcp_server())
        return 0

    # Validate config
    errors = config.validate()
    if errors and args.mode not in ("register-schema",):
        print("Configuration errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    config.print_status()

    # Instantiate modules (dependency injection)
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

    if args.mode == "discover":
        results = orchestrator.run_discovery_mode(
            args.start_block, args.end_block, args.max_agents
        )
        _print_summary(results)

    elif args.mode == "manual":
        agents = _load_manual_agents(args, discovery)
        if not agents:
            print("No agents to evaluate. Provide --agent-id or --address.", file=sys.stderr)
            return 1
        results = orchestrator.run_manual_mode(agents)
        _print_summary(results)

    elif args.mode == "register":
        agent_id, tx_hash = blockchain.register_agent(args.agent_uri)
        print(f"Registered! Agent ID: {agent_id}")
        print(f"TX: {blockchain.get_explorer_url(tx_hash)}")

    elif args.mode == "register-schema":
        schema_uid = blockchain.register_eas_schema()
        print(f"Schema registered! UID: {schema_uid}")
        print(f"Add to .env: EAS_SCHEMA_UID={schema_uid}")

    elif args.mode == "challenge":
        tx_hash = blockchain.create_validation_attestation(
            evaluation_id=args.evaluation_id,
            challenger_address=blockchain.evaluator_address,
            reason=args.reason,
            re_evaluation_required=True,
        )
        print(f"Challenge attestation created.")
        print(f"TX: {blockchain.get_explorer_url(tx_hash)}")

    return 0


def _load_manual_agents(args, discovery) -> list:
    """Load agents from CLI arguments."""
    agents = []

    if hasattr(args, "agent_id") and args.agent_id:
        agents.append(discovery.discover_agent_by_id(args.agent_id))

    if hasattr(args, "address") and args.address:
        agents.append(discovery.discover_agent_by_address(args.address))

    return agents


def _print_summary(results: list) -> None:
    """Print evaluation summary to stdout."""
    if not results:
        print("\nNo evaluations completed.")
        return

    print("\n" + "=" * 80)
    print("SENTINEL TRUST EVALUATION SUMMARY")
    print("=" * 80)
    print(f"{'Agent ID':<10} {'Score':>5} {'Conf':>5} {'I':>4} {'L':>4} {'O':>4} {'V':>4} {'State':<25} {'TX'}")
    print("-" * 80)
    for r in results:
        tx_display = r.tx_hash[:10] + "..." if r.tx_hash else "N/A"
        d = r.dimensions
        print(f"#{r.agent.agent_id:<9} {r.composite_score:>5} {r.evaluation_confidence:>5} "
              f"{d.identity_completeness:>4} {d.endpoint_liveness:>4} "
              f"{d.onchain_history:>4} {d.venice_trust_analysis:>4} "
              f"{r.state.value:<25} {tx_display}")
    print("=" * 80)
    print(f"Total: {len(results)} agents evaluated")


if __name__ == "__main__":
    sys.exit(main())
