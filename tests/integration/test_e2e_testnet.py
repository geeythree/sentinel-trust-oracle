"""End-to-end integration test: full pipeline against Base Sepolia.

Skipped in CI via SKIP_NETWORK_TESTS=1. Run locally with real keys:
    SKIP_NETWORK_TESTS=0 python3 -m pytest tests/integration/ -v
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

skip_network = pytest.mark.skipif(
    os.getenv("SKIP_NETWORK_TESTS", "1") == "1",
    reason="SKIP_NETWORK_TESTS is set — skipping live network tests",
)


@skip_network
class TestE2ETestnet:
    """Smoke test: discover agent #1 on Base Sepolia, run full pipeline."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        """Set up testnet config and orchestrator."""
        os.environ["USE_TESTNET"] = "true"
        import config as config_module
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

        self.orchestrator = Orchestrator(
            logger, discovery, agent_verifier, liveness_checker,
            onchain_analyzer, venice, scorer, blockchain,
        )
        self.discovery = discovery
        yield
        self.orchestrator.close()

    def test_evaluate_agent_1(self):
        """Evaluate agent #1 and assert valid output."""
        agent = self.discovery.discover_agent_by_id(1)
        result = self.orchestrator.evaluate_single(agent)

        assert 0 <= result.composite_score <= 100
        assert 0 <= result.evaluation_confidence <= 100
        # Must reach a terminal state
        terminal_states = {
            "PUBLISHED", "VERIFIED", "WITHHELD_LOW_CONFIDENCE",
            "PENDING_HUMAN_REVIEW", "FAILED",
        }
        assert result.state.value in terminal_states
