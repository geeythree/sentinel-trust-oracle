"""Unit tests for Validation Registry integration — config, blockchain, orchestrator, MCP, models."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import inspect


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestValidationRegistryConfig:
    """Validation Registry config properties."""

    def test_validation_registry_empty_by_default(self):
        """Default Config() has empty validation_registry."""
        c = cm.Config()
        assert c.validation_registry == ""

    def test_validation_registry_testnet_selection(self):
        """When USE_TESTNET=true and SEPOLIA addr set, validation_registry returns it."""
        c = cm.Config(
            USE_TESTNET=True,
            VALIDATION_REGISTRY_SEPOLIA="0x1234567890abcdef1234567890abcdef12345678",
        )
        assert c.validation_registry == "0x1234567890abcdef1234567890abcdef12345678"

    def test_validation_registry_mainnet_selection(self):
        """When USE_TESTNET=false and MAINNET addr set, validation_registry returns it."""
        c = cm.Config(
            USE_TESTNET=False,
            VALIDATION_REGISTRY_MAINNET="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        )
        assert c.validation_registry == "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"

    def test_create_config_reads_env_vars(self):
        """create_config() reads VALIDATION_REGISTRY_* from env."""
        old_m = os.environ.get("VALIDATION_REGISTRY_MAINNET")
        old_s = os.environ.get("VALIDATION_REGISTRY_SEPOLIA")
        try:
            os.environ["VALIDATION_REGISTRY_MAINNET"] = "0x" + "aa" * 20
            os.environ["VALIDATION_REGISTRY_SEPOLIA"] = "0x" + "bb" * 20
            c = cm.create_config()
            assert c.VALIDATION_REGISTRY_MAINNET == "0x" + "aa" * 20
            assert c.VALIDATION_REGISTRY_SEPOLIA == "0x" + "bb" * 20
        finally:
            if old_m is None:
                os.environ.pop("VALIDATION_REGISTRY_MAINNET", None)
            else:
                os.environ["VALIDATION_REGISTRY_MAINNET"] = old_m
            if old_s is None:
                os.environ.pop("VALIDATION_REGISTRY_SEPOLIA", None)
            else:
                os.environ["VALIDATION_REGISTRY_SEPOLIA"] = old_s


# ---------------------------------------------------------------------------
# Blockchain
# ---------------------------------------------------------------------------


class TestValidationRegistryBlockchain:
    """Validation Registry gating in BlockchainClient source."""

    def test_has_validation_registry_false_by_default(self):
        """Source: _validation_registry = None means has_validation_registry is False."""
        import blockchain
        source = inspect.getsource(blockchain.BlockchainClient.__init__)
        assert "_validation_registry = None" in source

    def test_validation_registry_gated_in_init(self):
        """Source: Validation Registry contract only created if config.validation_registry is set."""
        import blockchain
        source = inspect.getsource(blockchain.BlockchainClient.__init__)
        assert "if config.validation_registry:" in source

    def test_submit_validation_request_requires_registry(self):
        """Source: submit_validation_request raises BlockchainError when registry unavailable."""
        import blockchain
        source = inspect.getsource(blockchain.BlockchainClient.submit_validation_request)
        assert 'not self._validation_registry' in source
        assert 'BlockchainError' in source
        assert 'Validation Registry not available' in source

    def test_submit_validation_response_requires_registry(self):
        """Source: submit_validation_response raises BlockchainError when registry unavailable."""
        import blockchain
        source = inspect.getsource(blockchain.BlockchainClient.submit_validation_response)
        assert 'not self._validation_registry' in source
        assert 'BlockchainError' in source
        assert 'Validation Registry not available' in source

    def test_read_methods_return_empty_when_unavailable(self):
        """Source: get_validation_summary returns (0,0) and get_agent_validations returns [] when no registry."""
        import blockchain
        summary_source = inspect.getsource(blockchain.BlockchainClient.get_validation_summary)
        assert "return (0, 0)" in summary_source

        validations_source = inspect.getsource(blockchain.BlockchainClient.get_agent_validations)
        assert "return []" in validations_source


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TestValidationRegistryOrchestrator:
    """Validation Registry publish gating in orchestrator."""

    def test_publish_validation_registry_gated(self):
        """Source: _step_publish checks has_validation_registry before writing."""
        import orchestrator
        source = inspect.getsource(orchestrator.Orchestrator._step_publish)
        assert "has_validation_registry" in source

    def test_publish_validation_registry_nonblocking(self):
        """Source: Validation Registry write is wrapped in try/except (non-blocking)."""
        import orchestrator
        source = inspect.getsource(orchestrator.Orchestrator._step_publish)
        # The validation registry block is inside a try/except
        assert "Validation Registry write failed" in source


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


class TestValidationRegistryMCP:
    """Validation Registry tool in MCP server."""

    def test_check_validation_tool_listed(self):
        """Source: list_tools includes a Tool with name='check_validation'."""
        import mcp_server
        source = inspect.getsource(mcp_server.list_tools)
        assert 'name="check_validation"' in source

    def test_check_validation_handles_no_registry(self):
        """Source: check_validation returns 'not deployed' message when registry unavailable."""
        import mcp_server
        source = inspect.getsource(mcp_server.call_tool)
        assert "not deployed" in source


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestValidationRegistryModels:
    """Validation Registry model dataclasses."""

    def test_validation_result_dataclass(self):
        """ValidationResult can be constructed with all fields."""
        from models import ValidationResult
        vr = ValidationResult(
            request_hash="0xabc123",
            response_score=85,
            tag="trust-score",
            tx_hash="0xdef456",
        )
        assert vr.request_hash == "0xabc123"
        assert vr.response_score == 85
        assert vr.tag == "trust-score"
        assert vr.tx_hash == "0xdef456"

    def test_trust_verdict_has_validation_tx_hash(self):
        """TrustVerdict has a validation_tx_hash field, defaults to None."""
        from models import (
            TrustVerdict, DiscoveredAgent, TrustDimensions,
            EvaluationState, IdentityVerification, LivenessResult,
            OnchainAnalysis, VeniceEvaluation, VeniceParseMethod,
        )
        verdict = TrustVerdict(
            agent=DiscoveredAgent(agent_id=1, agent_uri="https://example.com", owner_address="0x" + "00" * 20),
            dimensions=TrustDimensions(identity_completeness=80, endpoint_liveness=70, onchain_history=60, venice_trust_analysis=75),
            composite_score=72,
            evaluation_confidence=80,
            state=EvaluationState.VERIFIED,
            identity_verification=IdentityVerification(success=True),
            liveness_result=LivenessResult(success=True),
            onchain_analysis=OnchainAnalysis(success=True),
            venice_evaluation=VeniceEvaluation(dimension="trust", score=75, reasoning="ok", parse_method=VeniceParseMethod.JSON_SCHEMA),
        )
        assert verdict.validation_tx_hash is None
        verdict.validation_tx_hash = "0xabc"
        assert verdict.validation_tx_hash == "0xabc"
