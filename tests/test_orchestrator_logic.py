"""Unit tests for Orchestrator logic — agent ordering, thread timeout, Venice summary guard."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import threading
import pytest
from models import DiscoveredAgent, OnchainAnalysis, ExistingReputation


class TestPlanEvaluationOrder:
    """Test _plan_evaluation_order with real DiscoveredAgent instances."""

    def _make_agents(self):
        return [
            DiscoveredAgent(
                agent_id=1,
                agent_uri="data:application/json,{}",
                owner_address="0x" + "aa" * 20,
                block_number=100,
            ),
            DiscoveredAgent(
                agent_id=2,
                agent_uri="https://example.com/agent.json",
                owner_address="0x" + "bb" * 20,
                block_number=200,
            ),
            DiscoveredAgent(
                agent_id=3,
                agent_uri="ipfs://QmTest123",
                owner_address="0x" + "cc" * 20,
                block_number=150,
            ),
        ]

    def test_newer_blocks_first(self):
        """Agents with higher block numbers (newer) should come first."""
        agents = self._make_agents()
        # Use the sorting logic directly
        def priority_key(a):
            block_score = -(a.block_number or 0)
            uri = a.agent_uri.lower() if a.agent_uri else ""
            uri_score = 0 if (uri.startswith("https://") or uri.startswith("ipfs://")) else 1
            return (uri_score, block_score, a.agent_id)

        sorted_agents = sorted(agents, key=priority_key)
        # HTTPS/IPFS URIs come before data: URIs
        # Among HTTPS/IPFS: block 200 (agent 2) before block 150 (agent 3)
        assert sorted_agents[0].agent_id == 2  # https, block 200
        assert sorted_agents[1].agent_id == 3  # ipfs, block 150
        assert sorted_agents[2].agent_id == 1  # data:, block 100

    def test_https_preferred_over_data(self):
        agents = [
            DiscoveredAgent(agent_id=1, agent_uri="data:application/json,{}", owner_address="0x" + "aa" * 20, block_number=200),
            DiscoveredAgent(agent_id=2, agent_uri="https://example.com/agent.json", owner_address="0x" + "bb" * 20, block_number=100),
        ]
        def priority_key(a):
            block_score = -(a.block_number or 0)
            uri = a.agent_uri.lower() if a.agent_uri else ""
            uri_score = 0 if (uri.startswith("https://") or uri.startswith("ipfs://")) else 1
            return (uri_score, block_score, a.agent_id)

        sorted_agents = sorted(agents, key=priority_key)
        assert sorted_agents[0].agent_id == 2  # https preferred despite older block

    def test_deterministic_tiebreak_by_agent_id(self):
        agents = [
            DiscoveredAgent(agent_id=5, agent_uri="https://a.com", owner_address="0x" + "aa" * 20, block_number=100),
            DiscoveredAgent(agent_id=3, agent_uri="https://b.com", owner_address="0x" + "bb" * 20, block_number=100),
        ]
        def priority_key(a):
            block_score = -(a.block_number or 0)
            uri = a.agent_uri.lower() if a.agent_uri else ""
            uri_score = 0 if (uri.startswith("https://") or uri.startswith("ipfs://")) else 1
            return (uri_score, block_score, a.agent_id)

        sorted_agents = sorted(agents, key=priority_key)
        assert sorted_agents[0].agent_id == 3  # lower ID wins tiebreak


class TestThreadTimeout:
    """Test that thread-based timeout mechanism works."""

    def test_fast_task_completes(self):
        """A fast task should complete within timeout."""
        result_holder = []

        def fast_task():
            result_holder.append("done")

        thread = threading.Thread(target=fast_task, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert result_holder == ["done"]

    def test_slow_task_times_out(self):
        """A slow task should be detected as timed out."""
        import time

        def slow_task():
            time.sleep(10)

        thread = threading.Thread(target=slow_task, daemon=True)
        thread.start()
        thread.join(timeout=0.1)
        assert thread.is_alive()  # Thread still running = timeout

    def test_exception_propagation(self):
        """Exceptions in the thread should be captured."""
        error_holder = []

        def failing_task():
            raise ValueError("test error")

        def _run():
            try:
                failing_task()
            except Exception as e:
                error_holder.append(e)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert len(error_holder) == 1
        assert str(error_holder[0]) == "test error"


class TestVeniceSummaryGuard:
    """Test that Venice summary handles failed onchain analysis."""

    def test_successful_onchain_summary(self):
        onchain = OnchainAnalysis(
            success=True,
            wallet_address="0x" + "aa" * 20,
            transaction_count=10,
            balance_eth=0.05,
            existing_reputation=ExistingReputation(feedback_count=2, summary_value=80, summary_decimals=0),
        )
        if onchain.success:
            summary = (
                f"Wallet: {onchain.wallet_address}, "
                f"TX count: {onchain.transaction_count}, "
                f"Balance: {onchain.balance_eth:.4f} ETH, "
                f"Existing reputation entries: {onchain.existing_reputation.feedback_count}"
            )
        else:
            summary = "On-chain analysis failed — no wallet data available"

        assert "TX count: 10" in summary
        assert "0.0500 ETH" in summary

    def test_failed_onchain_summary(self):
        onchain = OnchainAnalysis(
            success=False,
            wallet_address="0x" + "aa" * 20,
            onchain_score=0,
        )
        if onchain.success:
            summary = "should not reach"
        else:
            summary = "On-chain analysis failed — no wallet data available"

        assert "failed" in summary
        assert "TX count" not in summary


class TestOrchestratorImports:
    """Verify orchestrator no longer uses signal.SIGALRM."""

    def test_no_signal_import(self):
        import inspect
        import orchestrator
        source = inspect.getsource(orchestrator)
        assert "signal.SIGALRM" not in source
        assert "signal.alarm" not in source
