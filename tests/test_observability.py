"""Unit tests for observability — logger levels, noisy library filtering."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import logging
import pytest


class TestLogFilteringSetup:
    """Verify that _setup_logging sets noisy loggers to WARNING."""

    def test_web3_logger_set_to_warning(self):
        """After _setup_logging(), web3 logger should be WARNING or higher."""
        from main import _setup_logging
        _setup_logging()

        web3_logger = logging.getLogger("web3")
        assert web3_logger.level >= logging.WARNING

    def test_urllib3_logger_set_to_warning(self):
        """After _setup_logging(), urllib3 logger should be WARNING or higher."""
        from main import _setup_logging
        _setup_logging()

        urllib3_logger = logging.getLogger("urllib3")
        assert urllib3_logger.level >= logging.WARNING

    def test_root_logger_still_debug(self):
        """Root logger should remain at DEBUG level."""
        from main import _setup_logging
        _setup_logging()

        root = logging.getLogger()
        assert root.level == logging.DEBUG


class TestModuleLoggers:
    """Verify all key modules have proper loggers."""

    def test_blockchain_has_logger(self):
        import blockchain
        assert hasattr(blockchain, '_log')
        assert blockchain._log.name == 'blockchain'

    def test_venice_has_logger(self):
        import venice
        assert hasattr(venice, '_log')
        assert venice._log.name == 'venice'

    def test_agent_verifier_has_logger(self):
        import agent_verifier
        assert hasattr(agent_verifier, '_log')
        assert agent_verifier._log.name == 'agent_verifier'

    def test_orchestrator_has_logger(self):
        import orchestrator
        assert hasattr(orchestrator, '_log')
        assert orchestrator._log.name == 'orchestrator'

    def test_scorer_has_logger(self):
        import scorer
        assert hasattr(scorer, 'logger')
        assert scorer.logger.name == 'scorer'


class TestWatchStateLogging:
    """Verify watch state save uses logging, not silent pass."""

    def test_save_state_source_has_logging(self):
        """Confirm _save_state no longer has bare 'pass' on exception."""
        import inspect
        import main
        source = inspect.getsource(main._run_watch_mode)
        # Should have warning log, not bare pass
        assert "_watch_log.warning" in source
        # The except block should NOT have a bare pass
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "except Exception:" in line and i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if next_line == "pass":
                    pytest.fail("Found bare 'except Exception: pass' in _save_state")
