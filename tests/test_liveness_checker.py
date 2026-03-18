"""Unit tests for LivenessChecker — HTTP status code interpretation."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import pytest
from unittest.mock import patch, MagicMock
import requests
from liveness_checker import LivenessChecker


@pytest.fixture
def checker():
    logger = MagicMock()
    return LivenessChecker(logger)


# --- Status Code Interpretation ---

class TestStatusCodes:
    def _mock_head(self, status_code):
        resp = MagicMock()
        resp.status_code = status_code
        return resp

    @patch("liveness_checker.requests.head")
    def test_200_alive_full_score(self, mock_head, checker):
        mock_head.return_value = self._mock_head(200)
        result = checker._check_endpoint("https://api.test.com")
        assert result.status == "alive"
        assert result.score == 100

    @patch("liveness_checker.requests.head")
    def test_401_secured_full_score(self, mock_head, checker):
        """401 Unauthorized = endpoint exists AND is secured — this is GOOD."""
        mock_head.return_value = self._mock_head(401)
        result = checker._check_endpoint("https://api.test.com")
        assert result.status == "secured"
        assert result.score == 100

    @patch("liveness_checker.requests.head")
    def test_403_secured_full_score(self, mock_head, checker):
        mock_head.return_value = self._mock_head(403)
        result = checker._check_endpoint("https://api.test.com")
        assert result.status == "secured"
        assert result.score == 100

    @patch("liveness_checker.requests.head")
    def test_404_not_found_zero_score(self, mock_head, checker):
        mock_head.return_value = self._mock_head(404)
        result = checker._check_endpoint("https://api.test.com")
        assert result.status == "not_found"
        assert result.score == 0

    @patch("liveness_checker.requests.head")
    def test_500_server_error_low_score(self, mock_head, checker):
        mock_head.return_value = self._mock_head(500)
        result = checker._check_endpoint("https://api.test.com")
        assert result.status == "server_error"
        assert result.score == 20

    @patch("liveness_checker.requests.head")
    def test_301_redirect_partial_score(self, mock_head, checker):
        mock_head.return_value = self._mock_head(301)
        result = checker._check_endpoint("https://api.test.com")
        assert result.status == "redirect"
        assert result.score == 80

    @patch("liveness_checker.requests.head")
    def test_429_rate_limited(self, mock_head, checker):
        mock_head.return_value = self._mock_head(429)
        result = checker._check_endpoint("https://api.test.com")
        assert result.status == "rate_limited"
        assert result.score == 90

    @patch("liveness_checker.requests.head")
    def test_timeout_zero_score(self, mock_head, checker):
        mock_head.side_effect = requests.Timeout()
        result = checker._check_endpoint("https://api.test.com")
        assert result.status == "timeout"
        assert result.score == 0

    @patch("liveness_checker.requests.head")
    def test_connection_error_zero_score(self, mock_head, checker):
        mock_head.side_effect = requests.ConnectionError()
        result = checker._check_endpoint("https://api.test.com")
        assert result.status == "unreachable"
        assert result.score == 0

    @patch("liveness_checker.requests.head")
    @patch("liveness_checker.requests.get")
    def test_405_falls_back_to_get(self, mock_get, mock_head, checker):
        """405 Method Not Allowed → fallback to GET."""
        mock_head.return_value = self._mock_head(405)
        mock_get.return_value = self._mock_head(200)
        result = checker._check_endpoint("https://api.test.com")
        assert result.status == "alive"
        assert result.score == 100
        mock_get.assert_called_once()


# --- Manifest-level check ---

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

    def test_services_capped_at_20(self, checker):
        """Manifest with 50 services should only check first 20."""
        services = [{"endpoint": f"https://api{i}.test.com"} for i in range(50)]
        manifest = {"services": services}
        with patch("liveness_checker.requests.head") as mock_head:
            mock_head.return_value = MagicMock(status_code=200)
            result = checker.check(manifest)
        # Should have checked at most 20 endpoints (capped before processing)
        assert len(result.details) == 20
        assert result.endpoints_declared == 20

    @patch("liveness_checker.requests.head")
    def test_score_averaging_uses_round(self, mock_head, checker):
        """Verify round() not // is used for score averaging."""
        # 3 endpoints: scores 100, 100, 0 → average should be round(200/3) = 67
        responses = [MagicMock(status_code=200), MagicMock(status_code=200), MagicMock(status_code=404)]
        mock_head.side_effect = responses
        manifest = {"services": [
            {"endpoint": "https://a.com"},
            {"endpoint": "https://b.com"},
            {"endpoint": "https://c.com"},
        ]}
        result = checker.check(manifest)
        assert result.liveness_score == 67  # round(200/3) = 67, not 200//3 = 66
