"""Web3 wrapper for ERC-8004 and EAS interactions on Base."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional

from eth_account import Account
from tenacity import retry, stop_after_attempt, wait_exponential
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from config import config
from exceptions import BlockchainError, EASError
from logger import AgentLogger
from models import ActionType, AgentRole, TrustVerdict


class BlockchainClient:
    """web3.py wrapper for ERC-8004 and EAS interactions on Base."""

    def __init__(self, logger: AgentLogger) -> None:
        self._logger = logger
        self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))
        # Base is OP Stack -- needs POA middleware
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        # Load accounts
        self._operator_account = Account.from_key(config.OPERATOR_PRIVATE_KEY)
        self._evaluator_account = Account.from_key(config.EVALUATOR_PRIVATE_KEY)
        self._auditor_account = (
            Account.from_key(config.AUDITOR_PRIVATE_KEY)
            if config.AUDITOR_PRIVATE_KEY else None
        )

        # Load ABIs
        self._identity_abi = self._load_abi("IdentityRegistry.json")
        self._reputation_abi = self._load_abi("ReputationRegistry.json")
        self._eas_abi = self._load_abi("EAS.json")
        self._schema_registry_abi = self._load_abi("SchemaRegistry.json")

        # Contract instances
        self._identity_registry = self._w3.eth.contract(
            address=Web3.to_checksum_address(config.identity_registry),
            abi=self._identity_abi,
        )
        self._reputation_registry = self._w3.eth.contract(
            address=Web3.to_checksum_address(config.reputation_registry),
            abi=self._reputation_abi,
        )
        self._eas = self._w3.eth.contract(
            address=Web3.to_checksum_address(config.EAS_CONTRACT),
            abi=self._eas_abi,
        )

    def _load_abi(self, filename: str) -> list:
        abi_path = config.ABI_DIR / filename
        with open(abi_path, "r") as f:
            return json.load(f)

    # --- Identity Registry READ Functions ---

    def get_agent_by_id(self, agent_id: int) -> tuple[str, str]:
        """Read agent URI and owner from Identity Registry.

        Uses ownerOf() for the owner address. For the URI, scans Registered
        events since tokenURI() is not in the ABI. Scans recent blocks first
        (most agents are registered recently), then expands the range.
        """
        owner = self._identity_registry.functions.ownerOf(agent_id).call()

        # Get URI from Registered event
        event_sig = self._w3.keccak(text="Registered(uint256,string,address)")
        agent_id_topic = "0x" + hex(agent_id)[2:].zfill(64)
        latest = self._w3.eth.block_number
        uri = ""

        # Try progressively larger ranges: 10k, 100k, 500k, 2M blocks back
        for lookback in [10_000, 100_000, 500_000, 2_000_000]:
            start_block = max(0, latest - lookback)
            try:
                logs = self._w3.eth.get_logs({
                    "fromBlock": start_block,
                    "toBlock": latest,
                    "address": Web3.to_checksum_address(config.identity_registry),
                    "topics": [event_sig, agent_id_topic],
                })
                if logs:
                    event = self._identity_registry.events.Registered().process_log(logs[0])
                    uri = event["args"].get("agentURI", "")
                    break
            except Exception:
                continue

        return uri, owner

    def get_agent_wallet(self, agent_id: int) -> Optional[str]:
        """Read agentWallet from Identity Registry (may be zero address)."""
        try:
            wallet = self._identity_registry.functions.getAgentWallet(agent_id).call()
            if wallet == "0x0000000000000000000000000000000000000000":
                return None
            return wallet
        except Exception:
            return None

    def get_registered_events(self, from_block: int, to_block: int) -> list[dict]:
        """Get Registered events from Identity Registry using eth_getLogs."""
        # Use get_logs directly (public RPC nodes don't support eth_newFilter)
        event_sig = self._w3.keccak(text="Registered(uint256,string,address)")
        logs = self._w3.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": Web3.to_checksum_address(config.identity_registry),
            "topics": [event_sig],
        })
        # Decode logs using the contract event ABI
        decoded = []
        for log in logs:
            try:
                event = self._identity_registry.events.Registered().process_log(log)
                decoded.append(event)
            except Exception:
                continue
        return decoded

    def get_reputation_summary(self, agent_id: int) -> tuple[int, int, int]:
        """Read reputation summary. Returns (count, summaryValue, decimals)."""
        return self._reputation_registry.functions.getSummary(
            agent_id, [], "", ""
        ).call()

    def get_latest_block(self) -> int:
        """Get the latest block number."""
        return self._w3.eth.block_number

    # --- Identity Registry WRITE (Operator Wallet) ---

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=30),
    )
    def register_agent(self, agent_uri: str) -> tuple[int, str]:
        """Register an ERC-8004 identity using the OPERATOR wallet.

        Returns (agent_id, tx_hash) tuple.
        """
        tx = self._identity_registry.functions.register(
            agent_uri
        ).build_transaction(self._build_tx_params(self._operator_account))

        tx_hash = self._sign_and_send(tx, self._operator_account)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise BlockchainError(f"register() reverted. Tx: {tx_hash.hex()}")

        # Parse agent_id from Registered event
        logs = self._identity_registry.events.Registered().process_receipt(receipt)
        agent_id = logs[0]["args"]["agentId"] if logs else 0

        return agent_id, tx_hash.hex()

    # --- Reputation Registry (Evaluator Wallet) ---

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=30),
    )
    def give_feedback(
        self,
        agent_id: int,
        score: int,
        verdict: TrustVerdict,
        tag1: str = "trust-score",
        tag2: str = "agent-verification",
        endpoint: str = "",
    ) -> str:
        """Submit reputation feedback using the EVALUATOR wallet.

        CRITICAL: This MUST use a different wallet from the agent owner.
        """
        report_json = verdict.to_report_json()
        feedback_hash = Web3.keccak(text=report_json)
        # FIX: Use data: URI instead of placeholder ipfs:// URI
        feedback_uri = "data:application/json;base64," + base64.b64encode(report_json.encode()).decode()

        tx = self._reputation_registry.functions.giveFeedback(
            agent_id,
            score,
            0,                # uint8 valueDecimals (integer score)
            tag1,
            tag2,
            endpoint,
            feedback_uri,
            feedback_hash,
        ).build_transaction(self._build_tx_params(self._evaluator_account))

        tx_hash = self._sign_and_send(tx, self._evaluator_account)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise BlockchainError(f"giveFeedback reverted. Tx: {tx_hash.hex()}")

        return tx_hash.hex()

    # --- Self-Reputation (Auditor Wallet) ---

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=30),
    )
    def submit_self_feedback(
        self,
        aqe_agent_id: int,
        value: int,
        tag1: str = "evaluation_accuracy",
        tag2: str = "",
        reason: str = "",
    ) -> str:
        """Submit feedback about Sentinel itself using the AUDITOR wallet."""
        if not self._auditor_account:
            raise BlockchainError("Auditor wallet not configured")

        feedback_hash = Web3.keccak(text=reason or "self-assessment")
        feedback_uri = "data:application/json;base64," + base64.b64encode(
            (reason or "self-assessment").encode()
        ).decode()

        tx = self._reputation_registry.functions.giveFeedback(
            aqe_agent_id,
            value,
            0,
            tag1,
            tag2,
            "",
            feedback_uri,
            feedback_hash,
        ).build_transaction(self._build_tx_params(self._auditor_account))

        tx_hash = self._sign_and_send(tx, self._auditor_account)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise BlockchainError(f"Self-feedback reverted. Tx: {tx_hash.hex()}")

        return tx_hash.hex()

    # --- EAS Attestation (Validation Layer) ---

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=30),
    )
    def register_eas_schema(
        self,
        schema: str = "uint256 agentId, address agentOwner, uint8 trustScore, "
                       "uint8 confidence, bool identityVerified, uint8 endpointsLive, "
                       "uint8 endpointsDeclared, string evaluationId",
    ) -> str:
        """Register a custom EAS schema for trust verdicts. Returns schema UID."""
        schema_registry = self._w3.eth.contract(
            address=Web3.to_checksum_address(config.EAS_SCHEMA_REGISTRY),
            abi=self._schema_registry_abi,
        )

        tx = schema_registry.functions.register(
            schema,
            Web3.to_checksum_address("0x0000000000000000000000000000000000000000"),
            True,  # revocable
        ).build_transaction(self._build_tx_params(self._operator_account))

        tx_hash = self._sign_and_send(tx, self._operator_account)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise EASError(f"Schema registration reverted. Tx: {tx_hash.hex()}")

        # Parse schema UID from Registered event
        logs = schema_registry.events.Registered().process_receipt(receipt)
        schema_uid = logs[0]["args"]["uid"].hex() if logs else ""

        return schema_uid

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=30),
    )
    def create_trust_attestation(
        self,
        verdict: TrustVerdict,
    ) -> str:
        """Create an EAS attestation for a trust verdict."""
        if not config.EAS_SCHEMA_UID:
            raise EASError("EAS_SCHEMA_UID not configured. Run register-schema first.")

        # Encode attestation data matching the trust verdict schema
        data = self._w3.codec.encode(
            ["uint256", "address", "uint8", "uint8", "bool", "uint8", "uint8", "string"],
            [
                verdict.agent.agent_id,
                Web3.to_checksum_address(verdict.agent.owner_address),
                min(255, verdict.composite_score),
                min(255, verdict.evaluation_confidence),
                verdict.identity_verification.success,
                min(255, verdict.liveness_result.endpoints_live),
                min(255, verdict.liveness_result.endpoints_declared),
                verdict.evaluation_id,
            ],
        )

        # EAS attest() expects AttestationRequestData struct
        attestation_request = (
            bytes.fromhex(config.EAS_SCHEMA_UID.replace("0x", "")),  # schema
            (
                Web3.to_checksum_address(verdict.agent.owner_address),  # recipient
                0,  # expirationTime
                True,  # revocable
                bytes(32),  # refUID
                data,  # data
                0,  # value
            ),
        )

        tx = self._eas.functions.attest(
            attestation_request
        ).build_transaction(self._build_tx_params(self._evaluator_account))

        tx_hash = self._sign_and_send(tx, self._evaluator_account)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise EASError(f"EAS attestation reverted. Tx: {tx_hash.hex()}")

        return tx_hash.hex()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=30),
    )
    def create_validation_attestation(
        self,
        evaluation_id: str,
        challenger_address: str,
        reason: str,
        re_evaluation_required: bool,
    ) -> str:
        """Create an EAS attestation for a validation challenge."""
        if not config.EAS_SCHEMA_UID:
            raise EASError("EAS_SCHEMA_UID not configured. Run register-schema first.")

        eval_id_bytes = Web3.keccak(text=evaluation_id)
        challenger = Web3.to_checksum_address(challenger_address)

        data = self._w3.codec.encode(
            ["bytes32", "address", "string", "bool"],
            [eval_id_bytes, challenger, reason, re_evaluation_required],
        )

        attestation_request = (
            bytes.fromhex(config.EAS_SCHEMA_UID.replace("0x", "")),
            (
                Web3.to_checksum_address("0x0000000000000000000000000000000000000000"),
                0,
                True,
                bytes(32),
                data,
                0,
            ),
        )

        tx = self._eas.functions.attest(
            attestation_request
        ).build_transaction(self._build_tx_params(self._evaluator_account))

        tx_hash = self._sign_and_send(tx, self._evaluator_account)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise EASError(f"EAS attestation reverted. Tx: {tx_hash.hex()}")

        return tx_hash.hex()

    # --- Utilities ---

    def _build_tx_params(self, account) -> dict:
        """Build common transaction parameters."""
        return {
            "from": account.address,
            "nonce": self._w3.eth.get_transaction_count(account.address),
            "gas": 300_000,
            "maxFeePerGas": self._w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": self._w3.to_wei(0.001, "gwei"),
            "chainId": config.CHAIN_ID if not config.USE_TESTNET else 84532,
        }

    def _sign_and_send(self, tx: dict, account) -> bytes:
        """Sign and send a transaction."""
        signed = account.sign_transaction(tx)
        return self._w3.eth.send_raw_transaction(signed.raw_transaction)

    def get_explorer_url(self, tx_hash: str) -> str:
        """Return BaseScan URL for a transaction."""
        base = "https://sepolia.basescan.org" if config.USE_TESTNET else "https://basescan.org"
        return f"{base}/tx/{tx_hash}"

    @property
    def operator_address(self) -> str:
        return self._operator_account.address

    @property
    def evaluator_address(self) -> str:
        return self._evaluator_account.address

    @property
    def auditor_address(self) -> Optional[str]:
        return self._auditor_account.address if self._auditor_account else None
