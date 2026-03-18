"""Venice API client with structured output and 4-layer parse fallback."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import config
from exceptions import VeniceError
from logger import AgentLogger
from models import VeniceEvaluation, VeniceParseMethod

_log = logging.getLogger(__name__)

# Regex: matches {"score": N, "reasoning": "..."} even inside markdown fences
JSON_EXTRACT_PATTERN = re.compile(
    r'\{\s*"score"\s*:\s*(\d+)\s*,\s*"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
    re.DOTALL,
)

EVALUATION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "evaluation_result",
        "schema": {
            "type": "object",
            "properties": {
                "score": {"type": "integer"},
                "reasoning": {"type": "string"},
            },
            "required": ["score", "reasoning"],
            "additionalProperties": False,
        },
    },
}

TRUST_ANALYSIS_SYSTEM_PROMPT = """You are an AI agent trust evaluator.
Analyze the provided agent metadata, endpoint status, and onchain history to assess trustworthiness.
Consider: Is the agent manifest complete and professional? Are services functioning?
Does the onchain history suggest legitimacy? Are there any red flags?

You MUST respond with ONLY a JSON object in this exact format:
{"score": <integer 0-100>, "reasoning": "<explanation>"}

Score guide: 0 = highly suspicious, 50 = insufficient data to judge, 100 = strongly trustworthy.
Do NOT include any text outside the JSON object."""

CORRECTION_PROMPT = """Your previous response was not valid JSON.
Respond ONLY with a valid JSON object in this exact format, nothing else:
{"score": <integer 0-100>, "reasoning": "<your evaluation>"}"""


class VeniceClient:
    """Venice API client with 4-layer parse fallback."""

    def __init__(self, logger: AgentLogger) -> None:
        self._logger = logger
        self._base_url = config.VENICE_BASE_URL
        self._api_key = config.VENICE_API_KEY
        self._model = config.VENICE_MODEL
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        self._session = requests.Session()
        self._session.headers.update(self._headers)

    def close(self) -> None:
        """Close the HTTP session."""
        self._session.close()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=30),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _api_call(self, messages: list[dict], use_schema: bool = True) -> dict:
        """Single Venice API call with retry."""
        body: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": config.VENICE_TEMPERATURE,
            "max_tokens": config.VENICE_MAX_TOKENS,
        }
        if use_schema:
            body["response_format"] = EVALUATION_SCHEMA

        resp = self._session.post(
            f"{self._base_url}/chat/completions",
            json=body,
            timeout=(10, 120),  # (connect, read)
        )
        resp.raise_for_status()
        return resp.json()

    def evaluate_trust(
        self,
        manifest_json: str,
        liveness_summary: str,
        onchain_summary: str,
    ) -> VeniceEvaluation:
        """Private trust analysis of an agent."""
        messages = [
            {"role": "system", "content": TRUST_ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Agent Manifest:\n```json\n{manifest_json}\n```\n\n"
                f"Endpoint Liveness:\n{liveness_summary}\n\n"
                f"On-Chain History:\n{onchain_summary}"
            )},
        ]
        return self._evaluate(messages, dimension="trust_analysis")

    def _evaluate(
        self,
        messages: list[dict],
        dimension: str,
    ) -> VeniceEvaluation:
        """Core evaluation with 4-layer fallback.

        Layer 1: json_schema response_format
        Layer 2: Regex extraction from raw text
        Layer 3: Retry with correction prompt
        Layer 4: Neutral score (50) with venice_parse_failed=True
        """
        start_ms = time.monotonic()
        content = ""
        tokens_sent = 0
        tokens_received = 0

        # Layer 1: Try with json_schema
        try:
            raw_response = self._api_call(messages, use_schema=True)
            if "error" in raw_response:
                _log.warning("Venice API returned error: %s", raw_response["error"])
                raise ValueError(f"Venice API error: {raw_response['error']}")
            content = raw_response["choices"][0]["message"]["content"]
            tokens_sent = raw_response.get("usage", {}).get("prompt_tokens", 0)
            tokens_received = raw_response.get("usage", {}).get("completion_tokens", 0)

            parsed = json.loads(content)
            score = max(0, min(100, int(parsed["score"])))
            reasoning = str(parsed["reasoning"])

            latency = int((time.monotonic() - start_ms) * 1000)
            return VeniceEvaluation(
                dimension=dimension, score=score, reasoning=reasoning,
                parse_method=VeniceParseMethod.JSON_SCHEMA,
                tokens_sent=tokens_sent, tokens_received=tokens_received,
                latency_ms=latency,
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, IndexError) as e:
            _log.info("Layer 1 (json_schema) failed: %s — trying regex", e)
        except requests.RequestException as e:
            # API call itself failed after retries
            latency = int((time.monotonic() - start_ms) * 1000)
            return VeniceEvaluation(
                dimension=dimension, score=50,
                reasoning=f"Venice API call failed: {e}",
                parse_method=VeniceParseMethod.FALLBACK_NEUTRAL,
                venice_parse_failed=True, latency_ms=latency,
            )

        # Layer 2: Regex extraction
        try:
            match = JSON_EXTRACT_PATTERN.search(content)
            if match:
                score = max(0, min(100, int(match.group(1))))
                reasoning = match.group(2).replace('\\"', '"')
                latency = int((time.monotonic() - start_ms) * 1000)
                return VeniceEvaluation(
                    dimension=dimension, score=score, reasoning=reasoning,
                    parse_method=VeniceParseMethod.REGEX_EXTRACTION,
                    tokens_sent=tokens_sent, tokens_received=tokens_received,
                    latency_ms=latency,
                )
        except Exception as e:
            _log.info("Layer 2 (regex) failed: %s — trying correction prompt", e)

        # Layer 3: Retry with correction prompt
        try:
            messages_corrected = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": CORRECTION_PROMPT},
            ]
            raw_response = self._api_call(messages_corrected, use_schema=False)
            content2 = raw_response["choices"][0]["message"]["content"]
            parsed = json.loads(content2)
            score = max(0, min(100, int(parsed["score"])))
            reasoning = str(parsed["reasoning"])
            latency = int((time.monotonic() - start_ms) * 1000)
            return VeniceEvaluation(
                dimension=dimension, score=score, reasoning=reasoning,
                parse_method=VeniceParseMethod.RETRY_CORRECTION,
                tokens_sent=tokens_sent, tokens_received=tokens_received,
                latency_ms=latency,
            )
        except Exception as e:
            _log.warning("Layer 3 (correction prompt) failed: %s — falling back to neutral", e)

        # Layer 4: Neutral fallback
        latency = int((time.monotonic() - start_ms) * 1000)
        return VeniceEvaluation(
            dimension=dimension, score=50,
            reasoning="Venice parse failed after all fallback layers; neutral score assigned",
            parse_method=VeniceParseMethod.FALLBACK_NEUTRAL,
            venice_parse_failed=True,
            latency_ms=latency,
        )
