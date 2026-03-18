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

    # --- EAS (protocol-level predeploy on Base) ---
    EAS_CONTRACT: str = "0x4200000000000000000000000000000000000021"
    EAS_SCHEMA_REGISTRY: str = "0x4200000000000000000000000000000000000020"
    EAS_SCHEMA_UID: str = os.getenv("EAS_SCHEMA_UID", "")

    # --- Venice ---
    VENICE_BASE_URL: str = "https://api.venice.ai/api/v1"
    VENICE_MODEL: str = "qwen3-235b-a22b-instruct-2507"
    VENICE_MAX_TOKENS: int = 2048
    VENICE_TEMPERATURE: float = 0.1

    # --- Trust Scoring Weights (Sentinel) ---
    WEIGHT_IDENTITY: float = 0.20
    WEIGHT_LIVENESS: float = 0.25
    WEIGHT_ONCHAIN: float = 0.25
    WEIGHT_VENICE_TRUST: float = 0.30

    # --- Agent Discovery ---
    DISCOVERY_BLOCK_RANGE: int = 5000
    MAX_AGENTS_PER_RUN: int = 10

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

    def validate(self) -> list[str]:
        """Return list of missing required configuration keys."""
        errors = []
        if not self.OPERATOR_PRIVATE_KEY:
            errors.append("OPERATOR_PRIVATE_KEY is required")
        if not self.EVALUATOR_PRIVATE_KEY:
            errors.append("EVALUATOR_PRIVATE_KEY is required")
        if not self.VENICE_API_KEY:
            errors.append("VENICE_API_KEY is required")
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
        EAS_SCHEMA_UID=os.getenv("EAS_SCHEMA_UID", ""),
    )


# Module-level placeholder — set by main.py after arg parsing
config: Config = None  # type: ignore
