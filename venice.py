"""Venice API client with structured output and 4-layer parse fallback."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import requests
from tenacity import (
    RetryError,
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

TRUST_ANALYSIS_SYSTEM_PROMPT = """You are an AI agent trust evaluator performing private risk-categorized analysis. Evaluate the provided agent metadata, endpoint status, and on-chain history across specific risk categories.

## Risk Categories (evaluate each independently)

### 1. Identity Fraud Risk
- Is the manifest professional or placeholder/copied?
- Is the name generic ("test", "agent", "bot") or distinctive?
- Is the description substantive (>20 words, specific) or filler?
- Do services list real, distinct endpoints or filler domains (example.com, localhost)?

### 2. Endpoint Manipulation Risk
- Are endpoints actually responding or returning fake 200s?
- Do secured endpoints (401/403) suggest real auth infrastructure?
- Are there multiple distinct hostnames or all the same domain?
- Are response times consistent with real services (<2s) or suspicious?

### 3. On-chain Gaming Risk (Wash Trading Signals)
- Does tx count match the agent's apparent maturity?
- Is the balance proportional to activity (many txs but dust balance is suspicious)?
- Does existing reputation data corroborate or contradict other signals?
- Is there evidence of contract deployment (strong developer signal)?

### 4. Cross-Signal Consistency
- Do identity, liveness, and on-chain signals tell a coherent story?
- Flag specific contradictions (e.g. "500 txs but broken manifest", "polished manifest but 0 live endpoints")
- A new wallet with a polished manifest is unproven but not suspicious
- All signals weak but consistent → low score, not fraud

## Scoring Bands
- **85-100**: Low risk across all categories — professional, live, established, consistent
- **70-84**: Low-to-moderate risk — minor gaps in 1 category
- **55-69**: Moderate risk — mixed signals across categories
- **40-54**: Elevated risk — multiple weak categories
- **20-39**: High risk — placeholder/gaming indicators, dead endpoints, contradictions
- **0-19**: Critical risk — clear gaming attempt, all categories flagged

## Rules
- Do NOT default to 50. Use 30-45 for thin profiles with no red flags.
- Your reasoning MUST cite specific data points from each risk category you evaluated.
- Flag the highest-risk category explicitly.

You MUST respond with ONLY a JSON object in this exact format:
{"score": <integer 0-100>, "reasoning": "<risk-categorized explanation citing specific signals>"}
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
            timeout=(10, 180),  # (connect, read)
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
        except (requests.RequestException, RetryError) as e:
            # API unreachable or timed out after all retries — use neutral fallback
            latency = int((time.monotonic() - start_ms) * 1000)
            _log.warning("Venice API unavailable after retries (%s) — neutral score assigned", type(e).__name__)
            return VeniceEvaluation(
                dimension=dimension, score=50,
                reasoning=f"Venice API timed out after retries; neutral score assigned",
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
            # Update token counts from Layer 3 response
            tokens_sent = raw_response.get("usage", {}).get("prompt_tokens", tokens_sent)
            tokens_received = raw_response.get("usage", {}).get("completion_tokens", tokens_received)
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
