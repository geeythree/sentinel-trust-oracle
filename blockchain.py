"""Web3 wrapper for ERC-8004 and EAS interactions on Base."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
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
        """Read agent URI and owner from Identity Registry via tokenURI() and ownerOf()."""
        owner = self._identity_registry.functions.ownerOf(agent_id).call()

        # Try tokenURI() directly — fast, no event scanning needed
        try:
            token_uri_abi = [{
                "inputs": [{"name": "tokenId", "type": "uint256"}],
                "name": "tokenURI",
                "outputs": [{"name": "", "type": "string"}],
                "stateMutability": "view",
                "type": "function",
            }]
            contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(config.identity_registry),
                abi=token_uri_abi,
            )
            uri = contract.functions.tokenURI(agent_id).call()
            return uri, owner
        except Exception:
            pass

        return "", owner

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
        """Get Registered events from Identity Registry using eth_getLogs.

        Automatically chunks large ranges (>10k blocks) to avoid 413 errors
        from public RPC nodes.
        """
        event_sig = self._w3.keccak(text="Registered(uint256,string,address)")
        address = Web3.to_checksum_address(config.identity_registry)
        chunk_size = 10000

        all_logs = []
        start = from_block
        while start <= to_block:
            end = min(start + chunk_size - 1, to_block)
            try:
                logs = self._w3.eth.get_logs({
                    "fromBlock": start,
                    "toBlock": end,
                    "address": address,
                    "topics": [event_sig],
                })
                all_logs.extend(logs)
            except Exception:
                pass  # Skip failed chunks
            start = end + 1

        # Decode logs using the contract event ABI
        decoded = []
        for log in all_logs:
            try:
                event = self._identity_registry.events.Registered().process_log(log)
                decoded.append(event)
            except Exception:
                continue
        return decoded

    def get_reputation_summary(self, agent_id: int) -> tuple[int, int, int]:
        """Read reputation by counting NewFeedback events for this agent.

        The Reputation Registry is a minimal proxy and does not expose a
        getSummary() read function. Instead, we scan NewFeedback events
        emitted by giveFeedback() to derive the count and average value.
        Returns (count, averageValue, decimals=0).
        """
        try:
            latest = self._w3.eth.block_number
            from_block = max(0, latest - 50_000)  # scan recent ~2 days
            event_filter = self._reputation_registry.events.NewFeedback.create_filter(
                fromBlock=from_block, toBlock="latest",
                argument_filters={"agentId": agent_id},
            )
            logs = event_filter.get_all_entries()
            if not logs:
                return (0, 0, 0)
            count = len(logs)
            total_value = sum(log["args"]["value"] for log in logs)
            avg_value = total_value // count if count > 0 else 0
            return (count, avg_value, 0)
        except Exception:
            return (0, 0, 0)

    def get_latest_block(self) -> int:
        """Get the latest block number."""
        return self._w3.eth.block_number

    def get_agents_batch(self, agent_ids: list[int]) -> list[tuple[str, str]]:
        """Batch-read tokenURI + ownerOf for multiple agents using Multicall3.

        Multicall3 is deployed at the same address on all EVM chains:
        0xcA11bde05977b3631167028862bE2a173976CA11

        Returns list of (uri, owner) tuples in the same order as agent_ids.
        Falls back to sequential reads if Multicall3 call fails.

        TODO (production): Replace get_registered_events() eth_getLogs scanning
        with The Graph subgraph queries for scalable historical agent discovery:
          https://thegraph.com/docs/en/querying/querying-from-an-application/
        """
        if not agent_ids:
            return []

        MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
        # aggregate3 ABI — accepts (target, allowFailure, callData)[] tuples
        multicall3_abi = [{
            "inputs": [{"components": [
                {"name": "target", "type": "address"},
                {"name": "allowFailure", "type": "bool"},
                {"name": "callData", "type": "bytes"},
            ], "name": "calls", "type": "tuple[]"}],
            "name": "aggregate3",
            "outputs": [{"components": [
                {"name": "success", "type": "bool"},
                {"name": "returnData", "type": "bytes"},
            ], "name": "returnData", "type": "tuple[]"}],
            "stateMutability": "view",
            "type": "function",
        }]

        identity_addr = Web3.to_checksum_address(config.identity_registry)
        owner_of_sig = self._w3.keccak(text="ownerOf(uint256)")[:4]
        token_uri_sig = self._w3.keccak(text="tokenURI(uint256)")[:4]

        calls = []
        for agent_id in agent_ids:
            id_encoded = abi_encode(["uint256"], [agent_id])
            calls.append((identity_addr, True, owner_of_sig + id_encoded))
            calls.append((identity_addr, True, token_uri_sig + id_encoded))

        try:
            mc = self._w3.eth.contract(
                address=Web3.to_checksum_address(MULTICALL3),
                abi=multicall3_abi,
            )
            results = mc.functions.aggregate3(calls).call()

            output = []
            for i, agent_id in enumerate(agent_ids):
                owner_result = results[i * 2]
                uri_result = results[i * 2 + 1]

                owner = ""
                if owner_result[0] and len(owner_result[1]) >= 32:
                    owner = abi_decode(["address"], owner_result[1])[0]

                uri = ""
                if uri_result[0] and len(uri_result[1]) > 32:
                    try:
                        uri = abi_decode(["string"], uri_result[1])[0]
                    except Exception:
                        pass

                output.append((uri, owner))
            return output

        except Exception:
            # Fallback: sequential reads
            return [self.get_agent_by_id(aid) for aid in agent_ids]

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

        # Build a preliminary tx dict for gas estimation
        fn_call = self._reputation_registry.functions.giveFeedback(
            agent_id,
            score,
            0,                # uint8 valueDecimals (integer score)
            tag1,
            tag2,
            endpoint,
            feedback_uri,
            feedback_hash,
        )
        # Use contract call data for gas estimation
        estimate_tx = {"to": self._reputation_registry.address, "data": fn_call._encode_transaction_data()}
        tx = fn_call.build_transaction(self._build_tx_params(self._evaluator_account, estimate_tx))

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

        fn_call = self._reputation_registry.functions.giveFeedback(
            aqe_agent_id,
            value,
            0,
            tag1,
            tag2,
            "",
            feedback_uri,
            feedback_hash,
        )
        estimate_tx = {"to": self._reputation_registry.address, "data": fn_call._encode_transaction_data()}
        tx = fn_call.build_transaction(self._build_tx_params(self._auditor_account, estimate_tx))

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
        data = abi_encode(
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

        data = abi_encode(
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

    def _build_tx_params(self, account, tx_for_estimate: dict | None = None) -> dict:
        """Build common transaction parameters with dynamic gas estimation."""
        priority_fee = self._w3.to_wei(0.1, "gwei")  # Reasonable priority fee for Base
        base_fee = self._w3.eth.gas_price
        max_fee = max(base_fee * 2, priority_fee + base_fee)

        params = {
            "from": account.address,
            "nonce": self._w3.eth.get_transaction_count(account.address),
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "chainId": 84532 if config.USE_TESTNET else config.CHAIN_ID,
        }

        # Dynamic gas estimation with safety buffer and cap
        if tx_for_estimate:
            try:
                estimate_tx = {**tx_for_estimate, "from": account.address}
                estimated = self._w3.eth.estimate_gas(estimate_tx)
                # 30% buffer, capped at 1M to prevent runaway costs
                params["gas"] = min(int(estimated * 1.3), 1_000_000)
            except Exception:
                params["gas"] = 500_000  # fallback to safe default
        else:
            params["gas"] = 500_000

        return params

    def _sign_and_send(self, tx: dict, account) -> bytes:
        """Sign and send a transaction."""
        signed = account.sign_transaction(tx)
        return self._w3.eth.send_raw_transaction(signed.raw_transaction)

    def get_explorer_url(self, tx_hash: str) -> str:
        """Return BaseScan URL for a transaction."""
        base = "https://sepolia.basescan.org" if config.USE_TESTNET else "https://basescan.org"
        return f"{base}/tx/{tx_hash}"

    @property
    def w3(self) -> Web3:
        """Public access to web3 instance."""
        return self._w3

    @property
    def operator_address(self) -> str:
        return self._operator_account.address

    @property
    def evaluator_address(self) -> str:
        return self._evaluator_account.address

    @property
    def auditor_address(self) -> Optional[str]:
        return self._auditor_account.address if self._auditor_account else None
