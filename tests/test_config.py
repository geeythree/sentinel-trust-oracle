"""Unit tests for Config.validate() — real Config objects, no mocks."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import pytest
from config import Config


class TestConfigValidate:
    def test_valid_config_no_errors(self):
        cfg = Config(
            OPERATOR_PRIVATE_KEY="0x" + "ab" * 32,
            EVALUATOR_PRIVATE_KEY="0x" + "cd" * 32,
            VENICE_API_KEY="test-key",
        )
        errors = cfg.validate()
        assert errors == []

    def test_missing_operator_key(self):
        cfg = Config(
            OPERATOR_PRIVATE_KEY="",
            EVALUATOR_PRIVATE_KEY="0x" + "cd" * 32,
            VENICE_API_KEY="test-key",
        )
        errors = cfg.validate()
        assert any("OPERATOR_PRIVATE_KEY" in e for e in errors)

    def test_missing_evaluator_key(self):
        cfg = Config(
            OPERATOR_PRIVATE_KEY="0x" + "ab" * 32,
            EVALUATOR_PRIVATE_KEY="",
            VENICE_API_KEY="test-key",
        )
        errors = cfg.validate()
        assert any("EVALUATOR_PRIVATE_KEY" in e for e in errors)

    def test_missing_venice_key(self):
        cfg = Config(
            OPERATOR_PRIVATE_KEY="0x" + "ab" * 32,
            EVALUATOR_PRIVATE_KEY="0x" + "cd" * 32,
            VENICE_API_KEY="",
        )
        errors = cfg.validate()
        assert any("VENICE_API_KEY" in e for e in errors)

    def test_bad_rpc_url_format(self):
        cfg = Config(
            OPERATOR_PRIVATE_KEY="0x" + "ab" * 32,
            EVALUATOR_PRIVATE_KEY="0x" + "cd" * 32,
            VENICE_API_KEY="test-key",
            BASE_RPC_URL="ftp://invalid.com",
        )
        errors = cfg.validate()
        assert any("BASE_RPC_URL" in e for e in errors)

    def test_bad_sepolia_rpc_url_format(self):
        cfg = Config(
            OPERATOR_PRIVATE_KEY="0x" + "ab" * 32,
            EVALUATOR_PRIVATE_KEY="0x" + "cd" * 32,
            VENICE_API_KEY="test-key",
            BASE_SEPOLIA_RPC_URL="ws://bad.url",
        )
        errors = cfg.validate()
        assert any("BASE_SEPOLIA_RPC_URL" in e for e in errors)

    def test_weight_sum_not_one(self):
        cfg = Config(
            OPERATOR_PRIVATE_KEY="0x" + "ab" * 32,
            EVALUATOR_PRIVATE_KEY="0x" + "cd" * 32,
            VENICE_API_KEY="test-key",
            WEIGHT_IDENTITY=0.50,
            WEIGHT_LIVENESS=0.25,
            WEIGHT_ONCHAIN=0.25,
            WEIGHT_VENICE_TRUST=0.30,
        )
        errors = cfg.validate()
        assert any("weights must sum to 1.0" in e for e in errors)

    def test_valid_weights_pass(self):
        cfg = Config(
            OPERATOR_PRIVATE_KEY="0x" + "ab" * 32,
            EVALUATOR_PRIVATE_KEY="0x" + "cd" * 32,
            VENICE_API_KEY="test-key",
            WEIGHT_IDENTITY=0.20,
            WEIGHT_LIVENESS=0.20,
            WEIGHT_ONCHAIN=0.20,
            WEIGHT_VENICE_TRUST=0.25,
            WEIGHT_PROTOCOL=0.15,
        )
        errors = cfg.validate()
        assert not any("weights" in e for e in errors)

    def test_multiple_errors_accumulated(self):
        cfg = Config(
            OPERATOR_PRIVATE_KEY="",
            EVALUATOR_PRIVATE_KEY="",
            VENICE_API_KEY="",
            BASE_RPC_URL="bad-url",
        )
        errors = cfg.validate()
        assert len(errors) >= 4  # 3 missing keys + bad URL

    def test_default_temperature_is_zero(self):
        cfg = Config()
        assert cfg.VENICE_TEMPERATURE == 0.0
