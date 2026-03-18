"""Unit tests for LivenessChecker — real HTTP calls, real manifest dicts, session lifecycle."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import pytest
from pathlib import Path
from logger import AgentLogger
from liveness_checker import LivenessChecker


@pytest.fixture
def checker(tmp_path):
    logger = AgentLogger(tmp_path / "test_log.json", budget=15)
    lc = LivenessChecker(logger)
    yield lc
    lc.close()


# --- Manifest-level check (no network needed) ---

class TestManifestCheck:
    def test_empty_services(self, checker):
        result = checker.check({"services": []})
        assert result.success is True
        assert result.endpoints_declared == 0

    def test_no_services_key(self, checker):
        result = checker.check({})
        assert result.success is True
        assert result.endpoints_declared == 0

    def test_non_http_endpoint_neutral(self, checker):
        manifest = {"services": [{"endpoint": "stdio://agent"}]}
        result = checker.check(manifest)
        assert result.details[0].status == "non_http"
        assert result.details[0].score == 50

    def test_non_dict_services_skipped(self, checker):
        manifest = {"services": ["not-a-dict", 42, None]}
        result = checker.check(manifest)
        assert result.success is True
        assert result.endpoints_declared == 3
        assert len(result.details) == 0

    def test_empty_endpoint_gets_zero(self, checker):
        manifest = {"services": [{"endpoint": ""}]}
        result = checker.check(manifest)
        assert result.details[0].status == "missing"
        assert result.details[0].score == 0

    def test_services_capped_at_20(self, checker):
        """Manifest with 50 services should only process first 20."""
        services = [{"endpoint": "stdio://agent"} for _ in range(50)]
        manifest = {"services": services}
        result = checker.check(manifest)
        assert result.endpoints_declared == 20
        assert len(result.details) == 20

    def test_score_averaging_math(self, checker):
        """Verify round() averaging logic with non-HTTP endpoints."""
        # 3 non-http endpoints: all score 50 → average = 50
        manifest = {"services": [
            {"endpoint": "stdio://a"},
            {"endpoint": "stdio://b"},
            {"endpoint": "stdio://c"},
        ]}
        result = checker.check(manifest)
        assert result.liveness_score == 50

    def test_mixed_scores_averaging(self, checker):
        """Empty + non-HTTP: (0 + 50) / 2 = 25."""
        manifest = {"services": [
            {"endpoint": ""},
            {"endpoint": "stdio://agent"},
        ]}
        result = checker.check(manifest)
        assert result.liveness_score == 25


# --- Real HTTP calls (to httpbin.org) ---

class TestRealHTTP:
    """Tests using real HTTP calls. These require network access."""

    @pytest.mark.skipif(
        os.environ.get("SKIP_NETWORK_TESTS") == "1",
        reason="Network tests disabled",
    )
    def test_live_endpoint_200(self, checker):
        """httpbin.org/status/200 should return alive."""
        result = checker._check_endpoint("https://httpbin.org/status/200")
        assert result.status == "alive"
        assert result.score == 100

    @pytest.mark.skipif(
        os.environ.get("SKIP_NETWORK_TESTS") == "1",
        reason="Network tests disabled",
    )
    def test_not_found_404(self, checker):
        """httpbin.org/status/404 should return not_found."""
        result = checker._check_endpoint("https://httpbin.org/status/404")
        assert result.status == "not_found"
        assert result.score == 0

    @pytest.mark.skipif(
        os.environ.get("SKIP_NETWORK_TESTS") == "1",
        reason="Network tests disabled",
    )
    def test_server_error_500(self, checker):
        """httpbin.org/status/500 should return server_error."""
        result = checker._check_endpoint("https://httpbin.org/status/500")
        assert result.status == "server_error"
        assert result.score == 20


# --- Session lifecycle ---

class TestSessionLifecycle:
    def test_checker_has_session(self, checker):
        assert hasattr(checker, '_session')
        assert checker._session is not None

    def test_close_does_not_error(self, tmp_path):
        logger = AgentLogger(tmp_path / "test_log.json", budget=15)
        lc = LivenessChecker(logger)
        lc.close()  # Should not raise

    def test_close_method_exists(self, checker):
        assert hasattr(checker, 'close')
        assert callable(checker.close)
