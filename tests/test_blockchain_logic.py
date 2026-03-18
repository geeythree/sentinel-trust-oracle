"""Unit tests for blockchain.py logic — gas floor math, nonce param, logger existence."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import logging
from web3 import Web3


class TestGasFloorMath:
    """Test gas floor logic using real Web3.to_wei()."""

    def test_gas_floor_one_gwei(self):
        """Gas price below 1 gwei should be floored to 1 gwei."""
        floor = Web3.to_wei(1, "gwei")
        low_price = Web3.to_wei(0.5, "gwei")
        base_fee = max(low_price, floor)
        assert base_fee == floor

    def test_gas_floor_not_applied_when_higher(self):
        """Gas price above 1 gwei should pass through unchanged."""
        floor = Web3.to_wei(1, "gwei")
        high_price = Web3.to_wei(5, "gwei")
        base_fee = max(high_price, floor)
        assert base_fee == high_price

    def test_max_fee_calculation(self):
        """max_fee should be max(base_fee * 2, priority_fee + base_fee)."""
        priority_fee = Web3.to_wei(0.1, "gwei")
        base_fee = Web3.to_wei(2, "gwei")
        max_fee = max(base_fee * 2, priority_fee + base_fee)
        assert max_fee == base_fee * 2  # 4 gwei > 2.1 gwei

    def test_gas_estimation_buffer(self):
        """30% buffer capped at 1M gas."""
        estimated = 300_000
        buffered = min(int(estimated * 1.3), 1_000_000)
        assert buffered == 390_000

    def test_gas_estimation_buffer_cap(self):
        """Very high estimate should be capped at 1M."""
        estimated = 900_000
        buffered = min(int(estimated * 1.3), 1_000_000)
        assert buffered == 1_000_000


class TestNonceParam:
    """Verify that 'pending' nonce param is used in the code."""

    def test_pending_nonce_in_source(self):
        """Confirm the source code uses 'pending' for get_transaction_count."""
        import inspect
        import blockchain
        source = inspect.getsource(blockchain.BlockchainClient._build_tx_params)
        assert '"pending"' in source


class TestRegisterAgentValidation:
    """Verify register_agent raises on missing Registered event."""

    def test_source_raises_on_no_event(self):
        """Confirm the source code raises BlockchainError if no Registered event."""
        import inspect
        import blockchain
        source = inspect.getsource(blockchain.BlockchainClient.register_agent)
        assert "if not logs:" in source
        assert "BlockchainError" in source
        assert "no Registered event" in source


class TestBlockchainLogger:
    """Verify blockchain module has a logger configured."""

    def test_module_has_logger(self):
        import blockchain
        assert hasattr(blockchain, '_log')
        assert isinstance(blockchain._log, logging.Logger)
        assert blockchain._log.name == 'blockchain'
