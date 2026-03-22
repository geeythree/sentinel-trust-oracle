"""Centralized configuration. Loads from environment variables."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    """Immutable configuration loaded from environment."""

    # --- Network ---
    CHAIN_ID: int = 8453
    BASE_RPC_URL: str = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
    BASE_SEPOLIA_RPC_URL: str = os.getenv("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org")
    USE_TESTNET: bool = os.getenv("USE_TESTNET", "true").lower() == "true"

    # --- Wallets ---
    OPERATOR_PRIVATE_KEY: str = os.getenv("OPERATOR_PRIVATE_KEY", "")
    EVALUATOR_PRIVATE_KEY: str = os.getenv("EVALUATOR_PRIVATE_KEY", "")
    AUDITOR_PRIVATE_KEY: str = os.getenv("AUDITOR_PRIVATE_KEY", "")

    # --- API Keys ---
    BASESCAN_API_KEY: str = os.getenv("BASESCAN_API_KEY", "")
    VENICE_API_KEY: str = os.getenv("VENICE_API_KEY", "")

    # --- ERC-8004 Contract Addresses (Base Mainnet) ---
    IDENTITY_REGISTRY_MAINNET: str = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
    REPUTATION_REGISTRY_MAINNET: str = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"

    # --- ERC-8004 Contract Addresses (Base Sepolia) ---
    IDENTITY_REGISTRY_SEPOLIA: str = "0x8004A818BFB912233c491871b3d84c89A494BD9e"
    REPUTATION_REGISTRY_SEPOLIA: str = "0x8004B663056A597Dffe9eCcC1965A193B7388713"

    # --- ERC-8004 Validation Registry (not yet deployed) ---
    VALIDATION_REGISTRY_MAINNET: str = ""
    VALIDATION_REGISTRY_SEPOLIA: str = ""

    # --- EAS (protocol-level predeploy on Base) ---
    EAS_CONTRACT: str = "0x4200000000000000000000000000000000000021"
    EAS_SCHEMA_REGISTRY: str = "0x4200000000000000000000000000000000000020"
    EAS_SCHEMA_UID: str = os.getenv("EAS_SCHEMA_UID", "")

    # --- Venice ---
    VENICE_BASE_URL: str = "https://api.venice.ai/api/v1"
    VENICE_MODEL: str = "qwen3-235b-a22b-instruct-2507"
    VENICE_MAX_TOKENS: int = 2048
    VENICE_TEMPERATURE: float = 0.4

    # --- Trust Scoring Weights (4 base dimensions; must sum to 1.0) ---
    # Venice is applied as a multiplier, not a direct weight — see VENICE_MULTIPLIER_* below.
    WEIGHT_IDENTITY: float = 0.30   # 30%: raised — identity is the primary signal
    WEIGHT_LIVENESS: float = 0.25   # 25%: requires real infrastructure
    WEIGHT_ONCHAIN: float = 0.25    # 25%: transaction history is expensive to fake
    WEIGHT_VENICE_TRUST: float = 0.0  # deprecated direct weight — Venice is now a multiplier
    WEIGHT_PROTOCOL: float = 0.20   # 20%: MCP handshake proof-of-protocol

    # --- Venice Multiplier (applied to base composite, centered at score=50) ---
    # f(score) = clamp(0.3 + 1.4 * score/100, VENICE_MULTIPLIER_MIN, VENICE_MULTIPLIER_MAX)
    # f(0)=0.30, f(50)=1.00, f(100)=1.70 → clamped to [0.3, 1.5]
    VENICE_MULTIPLIER_MIN: float = 0.30   # floor: even the worst Venice score can't zero the composite
    VENICE_MULTIPLIER_MAX: float = 1.50   # ceiling: max boost for high Venice confidence

    # --- x402 Micropayment ---
    X402_ENABLED: bool = True   # Enable 402 payment gate on /api/evaluate (mainnet only)
    EVALUATION_FEE_USDC: int = 10_000  # 0.01 USDC (6 decimals)
    USDC_BASE_MAINNET: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    USDC_BASE_SEPOLIA: str = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

    # --- Veto Thresholds (cap composite when critical dimensions signal fraud) ---
    VETO_VENICE_THRESHOLD: int = 20    # Venice < 20 → composite capped at VETO_COMPOSITE_CAP_VENICE
    VETO_IDENTITY_THRESHOLD: int = 15  # identity < 15 → composite capped at VETO_COMPOSITE_CAP_IDENTITY
    VETO_COMPOSITE_CAP_VENICE: int = 30
    VETO_COMPOSITE_CAP_IDENTITY: int = 35

    # --- Agent Discovery ---
    DISCOVERY_BLOCK_RANGE: int = 50000
    MAX_AGENTS_PER_RUN: int = 50

    # --- IPFS Gateways ---
    IPFS_GATEWAYS: tuple = (
        "https://ipfs.io/ipfs/",
        "https://gateway.pinata.cloud/ipfs/",
        "https://cloudflare-ipfs.com/ipfs/",
    )

    # --- Liveness Check ---
    LIVENESS_TIMEOUT: int = 10
    MAX_SERVICES_PER_MANIFEST: int = 20  # cap to prevent DoS via 1000-endpoint manifests

    # --- Manifest Guardrails ---
    MAX_MANIFEST_BYTES: int = 102_400  # 100 KB — rejects oversized manifests before parsing

    # --- Autonomy ---
    AUTONOMOUS_MODE: bool = True  # Default: fully autonomous (no human review)

    # --- Pipeline ---
    TOOL_CALL_BUDGET: int = 15
    CONFIDENCE_THRESHOLD: int = 70
    SPREAD_HUMAN_REVIEW_THRESHOLD: int = 50
    SPREAD_PENALTY_THRESHOLD: int = 30
    HUMAN_REVIEW_TIMEOUT_SECONDS: int = 300

    # --- Retry (tenacity) ---
    RETRY_MAX_ATTEMPTS: int = 3
    RETRY_WAIT_MULTIPLIER: float = 1.0
    RETRY_WAIT_MAX: float = 30.0

    # --- Paths ---
    PROJECT_ROOT: Path = Path(__file__).parent
    ABI_DIR: Path = Path(__file__).parent / "abis"
    AGENT_LOG_PATH: Path = Path(__file__).parent / "agent_log.json"
    DASHBOARD_RESULTS_PATH: Path = Path(__file__).parent / "dashboard" / "results.json"

    @property
    def rpc_url(self) -> str:
        return self.BASE_SEPOLIA_RPC_URL if self.USE_TESTNET else self.BASE_RPC_URL

    @property
    def identity_registry(self) -> str:
        return self.IDENTITY_REGISTRY_SEPOLIA if self.USE_TESTNET else self.IDENTITY_REGISTRY_MAINNET

    @property
    def reputation_registry(self) -> str:
        return self.REPUTATION_REGISTRY_SEPOLIA if self.USE_TESTNET else self.REPUTATION_REGISTRY_MAINNET

    @property
    def validation_registry(self) -> str:
        addr = self.VALIDATION_REGISTRY_SEPOLIA if self.USE_TESTNET else self.VALIDATION_REGISTRY_MAINNET
        return addr if addr else ""

    def validate(self) -> list[str]:
        """Return list of missing or invalid configuration keys."""
        errors = []
        if not self.OPERATOR_PRIVATE_KEY:
            errors.append("OPERATOR_PRIVATE_KEY is required")
        if not self.EVALUATOR_PRIVATE_KEY:
            errors.append("EVALUATOR_PRIVATE_KEY is required")
        if not self.VENICE_API_KEY:
            errors.append("VENICE_API_KEY is required")

        # RPC URL format check
        for label, url in [("BASE_RPC_URL", self.BASE_RPC_URL),
                           ("BASE_SEPOLIA_RPC_URL", self.BASE_SEPOLIA_RPC_URL)]:
            if not url.startswith(("http://", "https://")):
                errors.append(f"{label} must start with http:// or https://")

        # Weight sum validation (4 base dimensions only; Venice is a multiplier)
        weight_sum = (self.WEIGHT_IDENTITY + self.WEIGHT_LIVENESS
                      + self.WEIGHT_ONCHAIN + self.WEIGHT_PROTOCOL)
        if abs(weight_sum - 1.0) > 0.001:
            errors.append(
                f"Base trust weights must sum to 1.0 (got {weight_sum:.4f}); "
                "Venice is a multiplier and is excluded from this sum"
            )

        return errors

    def print_status(self) -> None:
        """Print configuration status to stderr."""
        errors = self.validate()
        network = "Base Sepolia (testnet)" if self.USE_TESTNET else "Base Mainnet"
        print(f"Network: {network}", file=sys.stderr)
        print(f"Identity Registry: {self.identity_registry}", file=sys.stderr)
        print(f"Reputation Registry: {self.reputation_registry}", file=sys.stderr)
        if errors:
            print(f"Config warnings: {', '.join(errors)}", file=sys.stderr)


def create_config() -> Config:
    """Create config AFTER env vars are set (including CLI overrides).

    Reads os.getenv() HERE (at call time, after load_dotenv) rather than
    relying on dataclass defaults which are evaluated at import time.
    """
    load_dotenv()
    return Config(
        BASE_RPC_URL=os.getenv("BASE_RPC_URL", "https://mainnet.base.org"),
        BASE_SEPOLIA_RPC_URL=os.getenv("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org"),
        USE_TESTNET=os.getenv("USE_TESTNET", "true").lower() == "true",
        OPERATOR_PRIVATE_KEY=os.getenv("OPERATOR_PRIVATE_KEY", ""),
        EVALUATOR_PRIVATE_KEY=os.getenv("EVALUATOR_PRIVATE_KEY", ""),
        AUDITOR_PRIVATE_KEY=os.getenv("AUDITOR_PRIVATE_KEY", ""),
        BASESCAN_API_KEY=os.getenv("BASESCAN_API_KEY", ""),
        VENICE_API_KEY=os.getenv("VENICE_API_KEY", ""),
        VENICE_MODEL=os.getenv("VENICE_MODEL", "qwen3-235b-a22b-instruct-2507"),
        EAS_SCHEMA_UID=os.getenv("EAS_SCHEMA_UID", ""),
        VALIDATION_REGISTRY_MAINNET=os.getenv("VALIDATION_REGISTRY_MAINNET", ""),
        VALIDATION_REGISTRY_SEPOLIA=os.getenv("VALIDATION_REGISTRY_SEPOLIA", ""),
        AUTONOMOUS_MODE=os.getenv("AUTONOMOUS_MODE", "true").lower() == "true",
        WEIGHT_IDENTITY=float(os.getenv("WEIGHT_IDENTITY", "0.30")),
        WEIGHT_LIVENESS=float(os.getenv("WEIGHT_LIVENESS", "0.25")),
        WEIGHT_ONCHAIN=float(os.getenv("WEIGHT_ONCHAIN", "0.25")),
        WEIGHT_VENICE_TRUST=float(os.getenv("WEIGHT_VENICE_TRUST", "0.0")),
        WEIGHT_PROTOCOL=float(os.getenv("WEIGHT_PROTOCOL", "0.20")),
        VENICE_MULTIPLIER_MIN=float(os.getenv("VENICE_MULTIPLIER_MIN", "0.3")),
        VENICE_MULTIPLIER_MAX=float(os.getenv("VENICE_MULTIPLIER_MAX", "1.5")),
        VETO_VENICE_THRESHOLD=int(os.getenv("VETO_VENICE_THRESHOLD", "20")),
        VETO_IDENTITY_THRESHOLD=int(os.getenv("VETO_IDENTITY_THRESHOLD", "15")),
        VETO_COMPOSITE_CAP_VENICE=int(os.getenv("VETO_COMPOSITE_CAP_VENICE", "30")),
        VETO_COMPOSITE_CAP_IDENTITY=int(os.getenv("VETO_COMPOSITE_CAP_IDENTITY", "35")),
        DISCOVERY_BLOCK_RANGE=int(os.getenv("DISCOVERY_BLOCK_RANGE", "50000")),
        X402_ENABLED=os.getenv("X402_ENABLED", "true").lower() == "true",
        EVALUATION_FEE_USDC=int(os.getenv("EVALUATION_FEE_USDC", "10000")),
    )


# Module-level placeholder — set by main.py after arg parsing
config: Config = None  # type: ignore
