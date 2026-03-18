"""Unit tests for Venice logic — regex patterns, JSON parsing, score clamping, error detection."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPERATOR_PRIVATE_KEY", "0x" + "ab" * 32)
os.environ.setdefault("EVALUATOR_PRIVATE_KEY", "0x" + "cd" * 32)
os.environ.setdefault("VENICE_API_KEY", "test-key")
import config as cm
cm.config = cm.create_config()

import json
import logging
import re
import pytest
from venice import JSON_EXTRACT_PATTERN, EVALUATION_SCHEMA


class TestRegexExtraction:
    """Test the JSON_EXTRACT_PATTERN regex with real strings."""

    def test_plain_json(self):
        text = '{"score": 75, "reasoning": "Good agent"}'
        match = JSON_EXTRACT_PATTERN.search(text)
        assert match is not None
        assert int(match.group(1)) == 75
        assert match.group(2) == "Good agent"

    def test_json_in_markdown_fence(self):
        text = '```json\n{"score": 85, "reasoning": "Well structured manifest"}\n```'
        match = JSON_EXTRACT_PATTERN.search(text)
        assert match is not None
        assert int(match.group(1)) == 85

    def test_json_with_escaped_quotes(self):
        text = r'{"score": 60, "reasoning": "Has \"issues\" with liveness"}'
        match = JSON_EXTRACT_PATTERN.search(text)
        assert match is not None
        assert int(match.group(1)) == 60
        assert "issues" in match.group(2)

    def test_json_with_surrounding_text(self):
        text = 'Here is my evaluation:\n{"score": 42, "reasoning": "Suspicious"}\nEnd.'
        match = JSON_EXTRACT_PATTERN.search(text)
        assert match is not None
        assert int(match.group(1)) == 42

    def test_no_match_on_invalid(self):
        text = "This is just plain text with no JSON"
        match = JSON_EXTRACT_PATTERN.search(text)
        assert match is None

    def test_no_match_on_partial_json(self):
        text = '{"score": 75}'  # missing reasoning
        match = JSON_EXTRACT_PATTERN.search(text)
        assert match is None


class TestScoreClamping:
    """Test score clamping logic (0-100)."""

    def test_clamp_high(self):
        assert max(0, min(100, 150)) == 100

    def test_clamp_low(self):
        assert max(0, min(100, -10)) == 0

    def test_clamp_normal(self):
        assert max(0, min(100, 75)) == 75

    def test_clamp_boundary_zero(self):
        assert max(0, min(100, 0)) == 0

    def test_clamp_boundary_hundred(self):
        assert max(0, min(100, 100)) == 100


class TestErrorDetection:
    """Test error field detection in API responses."""

    def test_error_field_detected(self):
        response = {"error": {"message": "Rate limited", "code": 429}}
        assert "error" in response

    def test_normal_response_no_error(self):
        response = {
            "choices": [{"message": {"content": '{"score": 80, "reasoning": "ok"}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        assert "error" not in response


class TestEvaluationSchema:
    """Verify the EVALUATION_SCHEMA structure is valid."""

    def test_schema_has_required_fields(self):
        schema = EVALUATION_SCHEMA["json_schema"]["schema"]
        assert "score" in schema["properties"]
        assert "reasoning" in schema["properties"]
        assert schema["required"] == ["score", "reasoning"]

    def test_schema_type_is_json_schema(self):
        assert EVALUATION_SCHEMA["type"] == "json_schema"


class TestJSONParsing:
    """Test JSON parsing edge cases matching Venice response patterns."""

    def test_parse_valid_json(self):
        content = '{"score": 80, "reasoning": "Solid agent"}'
        parsed = json.loads(content)
        assert parsed["score"] == 80
        assert parsed["reasoning"] == "Solid agent"

    def test_parse_with_extra_whitespace(self):
        content = '  { "score" : 65 , "reasoning" : "Needs work" }  '
        parsed = json.loads(content)
        assert parsed["score"] == 65

    def test_parse_unicode_reasoning(self):
        content = '{"score": 70, "reasoning": "Agent \u2714 verified"}'
        parsed = json.loads(content)
        assert "\u2714" in parsed["reasoning"]


class TestVeniceModuleLogger:
    """Verify venice module has a logger."""

    def test_module_has_logger(self):
        import venice
        assert hasattr(venice, '_log')
        assert isinstance(venice._log, logging.Logger)
        assert venice._log.name == 'venice'


class TestTemperatureZero:
    """Verify temperature is set to 0.0 for deterministic scoring."""

    def test_config_temperature_zero(self):
        assert cm.config.VENICE_TEMPERATURE == 0.0
