"""All data structures for the Sentinel pipeline. Single source of truth."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EvaluationState(str, Enum):
    """State machine for evaluation lifecycle."""
    DISCOVERED = "DISCOVERED"
    PLANNING = "PLANNING"
    COMPILING = "COMPILING"          # reused as FETCH_IDENTITY
    ANALYZING = "ANALYZING"          # reused as CHECK_LIVENESS
    LLM_EVALUATING = "LLM_EVALUATING"
    SCORING = "SCORING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    WITHHELD_LOW_CONFIDENCE = "WITHHELD_LOW_CONFIDENCE"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class ActionType(str, Enum):
    TOOL_CALL = "Tool_Call"
    LLM_REASONING = "LLM_Reasoning"
    STATE_TRANSITION = "State_Transition"
    ON_CHAIN_TX = "On_Chain_Tx"
    HUMAN_INTERVENTION = "Human_Intervention"
    ERROR = "Error"
    RETRY = "Retry"


class AgentRole(str, Enum):
    PLANNER = "Planner"
    EVALUATOR = "Evaluator"
    VERIFIER = "Verifier"


class HumanDecision(str, Enum):
    PUBLISH = "publish"
    DISCARD = "discard"
    RE_EVALUATE = "re_evaluate"


class VeniceParseMethod(str, Enum):
    JSON_SCHEMA = "json_schema"
    REGEX_EXTRACTION = "regex_extraction"
    RETRY_CORRECTION = "retry_correction"
    FALLBACK_NEUTRAL = "fallback_neutral"


# --- Sentinel Dataclasses ---

@dataclass
class DiscoveredAgent:
    """Output of the discovery module. Input to the pipeline."""
    agent_id: int
    agent_uri: str
    owner_address: str
    chain_id: int = 8453
    block_number: Optional[int] = None
    discovery_source: str = "erc8004_events"


@dataclass
class ManifestValidation:
    """Validation result for an agent manifest."""
    fields_present: list[str] = field(default_factory=list)
    fields_missing: list[str] = field(default_factory=list)
    services_valid: bool = False


@dataclass
class IdentityVerification:
    """Output of agent_verifier.py."""
    success: bool
    manifest: Optional[dict] = None
    uri_resolved: str = ""
    fields_present: list[str] = field(default_factory=list)
    fields_missing: list[str] = field(default_factory=list)
    services_declared: int = 0
    identity_score: int = 0
    error_message: Optional[str] = None
    is_duplicate_manifest: bool = False
    duplicate_of_agent_id: Optional[int] = None


@dataclass
class EndpointCheck:
    """Single endpoint liveness check result."""
    endpoint: str
    status: str  # "alive", "secured", "timeout", "not_found", etc.
    http_code: int = 0
    score: int = 0
    response_time_ms: int = 0


@dataclass
class LivenessResult:
    """Aggregated output of liveness_checker.py."""
    success: bool
    endpoints_declared: int = 0
    endpoints_live: int = 0
    endpoints_secured: int = 0
    endpoints_dead: int = 0
    liveness_score: int = 0
    protocol_compliance_score: int = 0  # MCP protocol handshake check
    details: list[EndpointCheck] = field(default_factory=list)


@dataclass
class ExistingReputation:
    """Existing reputation data from ERC-8004 Reputation Registry."""
    feedback_count: int = 0
    summary_value: int = 0
    summary_decimals: int = 0


@dataclass
class OnchainAnalysis:
    """Output of onchain_analyzer.py."""
    success: bool
    wallet_address: str = ""
    transaction_count: int = 0
    balance_eth: float = 0.0
    has_contract_code: bool = False
    existing_reputation: ExistingReputation = field(default_factory=ExistingReputation)
    onchain_score: int = 0  # default 0; neutral baseline (50) set explicitly by analyzer on success


@dataclass
class VeniceEvaluation:
    """Output of a single Venice API evaluation call."""
    dimension: str
    score: int
    reasoning: str
    parse_method: VeniceParseMethod
    venice_parse_failed: bool = False
    model: str = "qwen3-235b-a22b-instruct-2507"
    tokens_sent: int = 0
    tokens_received: int = 0
    latency_ms: int = 0


@dataclass
class TrustDimensions:
    """All 5 trust dimension scores."""
    identity_completeness: int
    endpoint_liveness: int
    onchain_history: int
    venice_trust_analysis: int
    protocol_compliance: int = 0

    @property
    def as_list(self) -> list[int]:
        return [self.identity_completeness, self.endpoint_liveness,
                self.onchain_history, self.venice_trust_analysis,
                self.protocol_compliance]

    @property
    def spread(self) -> int:
        """Max difference between any two dimensions."""
        scores = self.as_list
        return max(scores) - min(scores)


@dataclass
class ValidationResult:
    """Result of an ERC-8004 Validation Registry request/response."""
    request_hash: str
    response_score: int
    tag: str
    tx_hash: str


@dataclass
class TrustVerdict:
    """Complete evaluation output. Primary data object."""
    agent: DiscoveredAgent
    dimensions: TrustDimensions
    composite_score: int
    evaluation_confidence: int
    state: EvaluationState
    identity_verification: IdentityVerification
    liveness_result: LivenessResult
    onchain_analysis: OnchainAnalysis
    venice_evaluation: VeniceEvaluation
    timestamp: str = ""
    evaluation_id: str = ""
    tool_calls_used: int = 0
    tool_calls_budget: int = 15
    tx_hash: Optional[str] = None
    attestation_uid: Optional[str] = None
    validation_tx_hash: Optional[str] = None
    human_decision: Optional[HumanDecision] = None
    is_self_evaluation: bool = False
    input_hash: Optional[str] = None

    def compute_input_hash(self) -> str:
        """SHA-256 hash of evaluation inputs for tamper-proof attestation."""
        import hashlib
        inputs = json.dumps({
            "manifest": self.identity_verification.manifest,
            "identity_score": self.identity_verification.identity_score,
            "liveness_score": self.liveness_result.liveness_score,
            "endpoints_declared": self.liveness_result.endpoints_declared,
            "endpoints_live": self.liveness_result.endpoints_live,
            "onchain_score": self.onchain_analysis.onchain_score,
            "tx_count": self.onchain_analysis.transaction_count,
            "balance_eth": self.onchain_analysis.balance_eth,
            "venice_score": self.venice_evaluation.score,
            "venice_parse_method": self.venice_evaluation.parse_method.value,
        }, sort_keys=True)
        return hashlib.sha256(inputs.encode()).hexdigest()

    def to_report_json(self) -> str:
        """Serialize for feedbackHash computation. Deterministic key order."""
        data = {
            "evaluation_id": self.evaluation_id,
            "agent_id": self.agent.agent_id,
            "agent_uri": self.agent.agent_uri,
            "owner_address": self.agent.owner_address,
            "composite_score": self.composite_score,
            "dimensions": {
                "identity_completeness": self.dimensions.identity_completeness,
                "endpoint_liveness": self.dimensions.endpoint_liveness,
                "onchain_history": self.dimensions.onchain_history,
                "venice_trust_analysis": self.dimensions.venice_trust_analysis,
                "protocol_compliance": self.dimensions.protocol_compliance,
            },
            "evaluation_confidence": self.evaluation_confidence,
            "timestamp": self.timestamp,
            "input_hash": self.input_hash,
        }
        return json.dumps(data, sort_keys=True)

    def to_dashboard_dict(self) -> dict:
        """For dashboard/results.json."""
        return {
            "agent_id": self.agent.agent_id,
            "agent_uri": self.agent.agent_uri,
            "owner_address": self.agent.owner_address,
            "composite_score": self.composite_score,
            "confidence": self.evaluation_confidence,
            "state": self.state.value,
            "dimensions": {
                "identity_completeness": self.dimensions.identity_completeness,
                "endpoint_liveness": self.dimensions.endpoint_liveness,
                "onchain_history": self.dimensions.onchain_history,
                "venice_trust_analysis": self.dimensions.venice_trust_analysis,
                "protocol_compliance": self.dimensions.protocol_compliance,
            },
            "spread": self.dimensions.spread,
            "endpoints": {
                "declared": self.liveness_result.endpoints_declared,
                "live": self.liveness_result.endpoints_live,
                "secured": self.liveness_result.endpoints_secured,
                "dead": self.liveness_result.endpoints_dead,
            },
            "tx_hash": self.tx_hash,
            "attestation_uid": self.attestation_uid,
            "tool_calls_used": self.tool_calls_used,
            "timestamp": self.timestamp,
            "discovery_source": self.agent.discovery_source,
            "chain_id": self.agent.chain_id,
            "venice": {
                "model": self.venice_evaluation.model,
                "tokens_sent": self.venice_evaluation.tokens_sent,
                "tokens_received": self.venice_evaluation.tokens_received,
                "latency_ms": self.venice_evaluation.latency_ms,
                "parse_method": self.venice_evaluation.parse_method.value,
            },
            "onchain": {
                "wallet_address": self.onchain_analysis.wallet_address,
                "tx_count": self.onchain_analysis.transaction_count,
                "balance_eth": self.onchain_analysis.balance_eth,
                "has_contract_code": self.onchain_analysis.has_contract_code,
                "reputation_entries": self.onchain_analysis.existing_reputation.feedback_count,
            },
            "identity": {
                "fields_present": self.identity_verification.fields_present,
                "fields_missing": self.identity_verification.fields_missing,
                "services_declared": self.identity_verification.services_declared,
                "uri_resolved": self.identity_verification.uri_resolved,
            },
            "is_self_evaluation": self.is_self_evaluation,
            "input_hash": self.input_hash,
        }

    def to_verdict_dict(self) -> dict:
        """For MCP tool response."""
        return {
            "agent_id": self.agent.agent_id,
            "verdict": "TRUSTED" if self.state == EvaluationState.PUBLISHED else self.state.value,
            "trust_score": self.composite_score,
            "confidence": self.evaluation_confidence,
            "identity_verified": self.identity_verification.success,
            "endpoints_declared": self.liveness_result.endpoints_declared,
            "endpoints_live": self.liveness_result.endpoints_live,
            "anomalies_detected": self.dimensions.spread > 50,
            "attestation_uid": self.attestation_uid,
            "basescan_url": (
                f"{'https://sepolia.basescan.org' if self.agent.chain_id == 84532 else 'https://basescan.org'}/tx/{self.tx_hash}"
                if self.tx_hash else None
            ),
        }


@dataclass
class LogEntry:
    """Single entry in agent_log.json. Strict schema for machine parsing."""
    timestamp: str
    agent_role: str
    action_type: str
    tool: Optional[str]
    payload: dict
    result: dict
    latency_ms: int
    compute_tokens: int
    compute_budget_remaining: int
