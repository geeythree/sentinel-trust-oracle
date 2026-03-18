"""CLI entry point for Sentinel — Autonomous Agent Trust Oracle."""
from __future__ import annotations

import argparse
import logging
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

    # Watch mode (continuous autonomous evaluation)
    watch = subparsers.add_parser("watch", help="Continuously discover and evaluate agents (autonomous loop)")
    watch.add_argument("--interval", type=int, default=300, help="Seconds between discovery rounds (default: 300)")
    watch.add_argument("--max-agents", type=int, default=5, help="Max agents per round")

    # Self-evaluation mode
    self_eval = subparsers.add_parser("self-eval", help="Sentinel evaluates itself (circular trust demo)")
    self_eval.add_argument("--agent-id", type=int, default=33465, help="Sentinel's own ERC-8004 agent ID")

    # MCP server mode
    subparsers.add_parser("mcp-server", help="Start MCP server (stdio transport)")

    # Common arguments
    parser.add_argument("--testnet", action="store_true", default=False, help="Use Base Sepolia")
    parser.add_argument("--mainnet", action="store_true", help="Use Base Mainnet")

    return parser.parse_args()


def _setup_logging() -> None:
    """Configure Python logging: INFO to stderr, DEBUG to sentinel.log."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console: INFO-level, concise format
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(console)

    # File: DEBUG-level, full timestamps
    fh = logging.FileHandler("sentinel.log", mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)


def main() -> int:
    args = parse_args()
    _setup_logging()

    # FIX: Set env vars BEFORE creating config
    # Only override if explicitly passed — otherwise respect .env
    if args.mainnet:
        os.environ["USE_TESTNET"] = "false"
    elif args.testnet:
        os.environ["USE_TESTNET"] = "true"

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

    elif args.mode == "watch":
        _run_watch_mode(orchestrator, blockchain, args)

    elif args.mode == "self-eval":
        _run_self_evaluation(orchestrator, blockchain, args)

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


def _run_watch_mode(orchestrator, blockchain, args) -> None:
    """Continuous autonomous loop: discover → evaluate → sleep → repeat.

    Persists state (last_block, round_num) to .watch_state.json so that
    the loop can resume from where it left off after a crash.
    """
    import json
    import time
    from pathlib import Path
    from config import config

    state_file = Path(".watch_state.json")

    # Restore state from previous run if available
    round_num = 0
    last_block = blockchain.get_latest_block() - config.DISCOVERY_BLOCK_RANGE
    if state_file.exists():
        try:
            saved = json.loads(state_file.read_text())
            last_block = saved.get("last_block", last_block)
            round_num = saved.get("round_num", 0)
            print(f"Restored watch state: last_block={last_block}, round={round_num}")
        except (json.JSONDecodeError, KeyError):
            pass

    def _save_state():
        try:
            state_file.write_text(json.dumps({
                "last_block": last_block,
                "round_num": round_num,
            }))
        except Exception:
            pass

    print(f"\nSentinel WATCH mode — evaluating every {args.interval}s (Ctrl+C to stop)")
    print("=" * 60)

    consecutive_failures = 0
    max_consecutive_failures = 5

    try:
        while True:
            round_num += 1
            try:
                current_block = blockchain.get_latest_block()
                print(f"\n[Round {round_num}] Scanning blocks {last_block}..{current_block}")

                results = orchestrator.run_discovery_mode(
                    last_block, current_block, args.max_agents
                )
                _print_summary(results)

                last_block = current_block + 1
                consecutive_failures = 0  # reset on success
                _save_state()

                published = sum(1 for r in results if r.tx_hash)
                print(f"\n[Round {round_num}] {len(results)} evaluated, {published} published. "
                      f"Next scan in {args.interval}s...")
            except KeyboardInterrupt:
                raise  # re-raise to outer handler
            except Exception as e:
                consecutive_failures += 1
                backoff = min(args.interval * consecutive_failures, 900)
                print(f"\n[Round {round_num}] ERROR: {e}")
                print(f"  Consecutive failures: {consecutive_failures}/{max_consecutive_failures}")
                if consecutive_failures >= max_consecutive_failures:
                    print(f"  Too many failures. Stopping watch mode.")
                    break
                print(f"  Retrying in {backoff}s...")
                _save_state()
                time.sleep(backoff)
                continue

            time.sleep(args.interval)
    except KeyboardInterrupt:
        _save_state()
        print(f"\nWatch mode stopped after {round_num} rounds. State saved.")


def _run_self_evaluation(orchestrator, blockchain, args) -> None:
    """Sentinel evaluates itself — circular trust demonstration."""
    from agent_discovery import AgentDiscovery

    sentinel_id = args.agent_id
    print(f"\nSentinel SELF-EVALUATION — Agent #{sentinel_id}")
    print("=" * 60)
    print("Sentinel is evaluating its own identity, liveness, and trust.")
    print("This demonstrates a circular trust model: a trust oracle that")
    print("practices what it preaches.\n")

    # Discover ourselves
    discovery = orchestrator._discovery
    sentinel = discovery.discover_agent_by_id(sentinel_id)

    # Run full evaluation pipeline
    result = orchestrator.evaluate_single(sentinel)
    _print_summary([result])

    # Submit self-feedback via AUDITOR wallet
    if result.tx_hash:
        print(f"\nSelf-evaluation published via EVALUATOR wallet.")
        try:
            reason = (f"Self-assessment: score={result.composite_score}, "
                      f"confidence={result.evaluation_confidence}, "
                      f"identity={result.dimensions.identity_completeness}, "
                      f"liveness={result.dimensions.endpoint_liveness}")
            self_tx = blockchain.submit_self_feedback(
                aqe_agent_id=sentinel_id,
                value=result.composite_score,
                tag1="self_assessment",
                tag2=f"conf_{result.evaluation_confidence}",
                reason=reason,
            )
            print(f"Self-feedback submitted via AUDITOR wallet.")
            print(f"TX: {blockchain.get_explorer_url(self_tx)}")
        except Exception as e:
            print(f"Self-feedback skipped (AUDITOR not configured or error): {e}")


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
