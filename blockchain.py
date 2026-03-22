"""Web3 wrapper for ERC-8004 and EAS interactions on Base."""
from __future__ import annotations

import base64
import json
import logging
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

_log = logging.getLogger(__name__)


class BlockchainClient:
    """web3.py wrapper for ERC-8004 and EAS interactions on Base."""

    def __init__(self, logger: AgentLogger) -> None:
        self._logger = logger
        self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))
        # Base is OP Stack -- needs POA middleware
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        # Load accounts (defer crash to write operations if keys missing)
        try:
            self._operator_account = Account.from_key(config.OPERATOR_PRIVATE_KEY)
        except (ValueError, TypeError):
            _log.warning("OPERATOR_PRIVATE_KEY not set — write operations will fail")
            self._operator_account = None
        try:
            self._evaluator_account = Account.from_key(config.EVALUATOR_PRIVATE_KEY)
        except (ValueError, TypeError):
            _log.warning("EVALUATOR_PRIVATE_KEY not set — write operations will fail")
            self._evaluator_account = None
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

        # Validation Registry (conditionally loaded — not yet deployed)
        self._validation_registry = None
        if config.validation_registry:
            try:
                self._validation_abi = self._load_abi("ValidationRegistry.json")
                self._validation_registry = self._w3.eth.contract(
                    address=Web3.to_checksum_address(config.validation_registry),
                    abi=self._validation_abi,
                )
            except Exception:
                _log.warning("Validation Registry init failed", exc_info=True)

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
            _log.warning("tokenURI(%d) call failed", agent_id, exc_info=True)

        return "", owner

    def get_agent_wallet(self, agent_id: int) -> Optional[str]:
        """Read wallet for an agent — uses ownerOf() (ERC-8004 owner is the wallet)."""
        try:
            owner = self._identity_registry.functions.ownerOf(agent_id).call()
            if owner == "0x0000000000000000000000000000000000000000":
                return None
            return owner
        except Exception:
            _log.warning("ownerOf(%d) call failed", agent_id, exc_info=True)
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
                _log.warning("get_logs chunk %d-%d failed", start, end, exc_info=True)
            start = end + 1

        # Decode logs using the contract event ABI
        decoded = []
        for log in all_logs:
            try:
                event = self._identity_registry.events.Registered().process_log(log)
                decoded.append(event)
            except Exception:
                _log.warning("Failed to decode Registered event log", exc_info=True)
                continue
        return decoded

    def get_reputation_summary(self, agent_id: int) -> tuple[int, int, int]:
        """Read reputation by counting NewFeedback events for this agent.

        The Reputation Registry is a minimal proxy and does not expose a
        getSummary() read function. Instead, we scan NewFeedback events
        emitted by giveFeedback() to derive the count and average value.
        Returns (count, averageValue, decimals=0).

        Uses eth_getLogs (not eth_newFilter) for public RPC compatibility.
        """
        try:
            latest = self._w3.eth.block_number
            from_block = max(0, latest - 50_000)  # scan recent ~2 days
            address = Web3.to_checksum_address(config.reputation_registry)

            # Use eth_getLogs directly (public RPCs often reject eth_newFilter)
            event_sig = self._w3.keccak(text="NewFeedback(uint256,address,int128,string,string)")
            # agentId is indexed topic[1]
            agent_id_topic = "0x" + abi_encode(["uint256"], [agent_id]).hex()

            # Chunk to avoid 413 from public RPCs (same pattern as get_registered_events)
            chunk_size = 10_000
            logs = []
            start = from_block
            while start <= latest:
                end = min(start + chunk_size - 1, latest)
                try:
                    chunk_logs = self._w3.eth.get_logs({
                        "fromBlock": start,
                        "toBlock": end,
                        "address": address,
                        "topics": [event_sig, agent_id_topic],
                    })
                    logs.extend(chunk_logs)
                except Exception:
                    _log.warning("get_reputation_summary chunk %d-%d failed", start, end, exc_info=True)
                start = end + 1

            if not logs:
                return (0, 0, 0)

            decoded = []
            for log in logs:
                try:
                    event = self._reputation_registry.events.NewFeedback().process_log(log)
                    decoded.append(event)
                except Exception:
                    _log.warning("Failed to decode NewFeedback event", exc_info=True)
                    continue

            if not decoded:
                return (0, 0, 0)

            count = len(decoded)
            total_value = sum(e["args"]["value"] for e in decoded)
            avg_value = total_value // count if count > 0 else 0
            return (count, avg_value, 0)
        except Exception:
            _log.warning("get_reputation_summary(%d) failed", agent_id, exc_info=True)
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
                        _log.warning("Failed to decode tokenURI for agent %d", agent_id, exc_info=True)

                output.append((uri, owner))
            return output

        except Exception:
            _log.warning("Multicall3 batch read failed, falling back to sequential", exc_info=True)
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
        if not self._operator_account:
            raise BlockchainError("OPERATOR_PRIVATE_KEY not configured")
        tx = self._identity_registry.functions.register(
            agent_uri
        ).build_transaction(self._build_tx_params(self._operator_account))

        tx_hash = self._sign_and_send(tx, self._operator_account)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise BlockchainError(f"register() reverted. Tx: {tx_hash.hex()}")

        # Parse agent_id from Registered event
        logs = self._identity_registry.events.Registered().process_receipt(receipt)
        if not logs:
            raise BlockchainError(
                f"register() succeeded but no Registered event emitted. Tx: {tx_hash.hex()}"
            )
        agent_id = logs[0]["args"]["agentId"]

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
        if not self._evaluator_account:
            raise BlockchainError("EVALUATOR_PRIVATE_KEY not configured")
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
        sentinel_agent_id: int,
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
            sentinel_agent_id,
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
        if not self._evaluator_account:
            raise EASError("EVALUATOR_PRIVATE_KEY not configured")
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

        # Parse real attestation UID from Attested event
        try:
            logs = self._eas.events.Attested().process_receipt(receipt)
            if logs:
                return "0x" + logs[0]["args"]["uid"].hex()
        except Exception:
            _log.warning("Failed to parse Attested event, falling back to tx hash", exc_info=True)
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

        # Parse real attestation UID from Attested event
        try:
            logs = self._eas.events.Attested().process_receipt(receipt)
            if logs:
                return "0x" + logs[0]["args"]["uid"].hex()
        except Exception:
            _log.warning("Failed to parse Attested event, falling back to tx hash", exc_info=True)
        return tx_hash.hex()

    # --- Validation Registry (ERC-8004, gated behind deployment) ---

    @property
    def has_validation_registry(self) -> bool:
        return self._validation_registry is not None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=30),
    )
    def submit_validation_request(
        self, agent_id: int, request_uri: str
    ) -> tuple[str, bytes]:
        """Submit a validation request via OPERATOR wallet (agent owner initiates).

        The contract requires msg.sender == ownerOf(agentId). OPERATOR owns Sentinel's
        agent and calls validationRequest(evaluatorAddress, agentId, ...) to designate
        the EVALUATOR as the responder. Only works for agents Sentinel owns.

        Returns (tx_hash, request_hash).
        """
        if not self._validation_registry:
            raise BlockchainError("Validation Registry not available")
        if not self._operator_account:
            raise BlockchainError("OPERATOR_PRIVATE_KEY not configured")
        if not self._evaluator_account:
            raise BlockchainError("EVALUATOR_PRIVATE_KEY not configured")

        request_hash = Web3.keccak(text=request_uri)

        fn_call = self._validation_registry.functions.validationRequest(
            self._evaluator_account.address,  # validator who will respond
            agent_id,
            request_uri,
            request_hash,
        )
        estimate_tx = {
            "from": self._operator_account.address,
            "to": self._validation_registry.address,
            "data": fn_call._encode_transaction_data(),
        }
        tx = fn_call.build_transaction(
            self._build_tx_params(self._operator_account, estimate_tx)
        )

        tx_hash = self._sign_and_send(tx, self._operator_account)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise BlockchainError(f"validationRequest reverted. Tx: {tx_hash.hex()}")

        return tx_hash.hex(), request_hash

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=30),
    )
    def submit_validation_response(
        self,
        request_hash: bytes,
        score: int,
        response_uri: str,
        tag: str = "trust-score",
    ) -> str:
        """Submit a validation response via EVALUATOR wallet. Returns tx_hash."""
        if not self._validation_registry:
            raise BlockchainError("Validation Registry not available")
        if not self._evaluator_account:
            raise BlockchainError("EVALUATOR_PRIVATE_KEY not configured")

        response_hash = Web3.keccak(text=response_uri)
        response_val = min(255, max(0, score))  # uint8

        fn_call = self._validation_registry.functions.validationResponse(
            request_hash,
            response_val,
            response_uri,
            response_hash,
            tag,
        )
        estimate_tx = {
            "to": self._validation_registry.address,
            "data": fn_call._encode_transaction_data(),
        }
        tx = fn_call.build_transaction(
            self._build_tx_params(self._evaluator_account, estimate_tx)
        )

        tx_hash = self._sign_and_send(tx, self._evaluator_account)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] != 1:
            raise BlockchainError(f"validationResponse reverted. Tx: {tx_hash.hex()}")

        return tx_hash.hex()

    def get_validation_summary(
        self, agent_id: int, tag: str = "trust-score"
    ) -> tuple[int, int]:
        """Read validation summary. Returns (count, avgResponse)."""
        if not self._validation_registry:
            return (0, 0)
        try:
            validators = [self._evaluator_account.address] if self._evaluator_account else []
            count, avg = self._validation_registry.functions.getSummary(
                agent_id, validators, tag
            ).call()
            return (count, avg)
        except Exception:
            _log.warning("getValidationSummary(%d) failed", agent_id, exc_info=True)
            return (0, 0)

    def get_validation_status(self, request_hash: bytes) -> dict:
        """Read validation status for a request hash."""
        if not self._validation_registry:
            return {}
        try:
            result = self._validation_registry.functions.getValidationStatus(
                request_hash
            ).call()
            return {
                "validator": result[0],
                "agent_id": result[1],
                "response": result[2],
                "response_hash": result[3].hex() if isinstance(result[3], bytes) else result[3],
                "tag": result[4],
                "timestamp": result[5],
            }
        except Exception:
            _log.warning("getValidationStatus failed", exc_info=True)
            return {}

    def get_agent_validations(self, agent_id: int) -> list[bytes]:
        """Read all validation request hashes for an agent."""
        if not self._validation_registry:
            return []
        try:
            return self._validation_registry.functions.getAgentValidations(
                agent_id
            ).call()
        except Exception:
            _log.warning("getAgentValidations(%d) failed", agent_id, exc_info=True)
            return []

    # --- Utilities ---

    def _build_tx_params(self, account, tx_for_estimate: dict | None = None) -> dict:
        """Build common transaction parameters with dynamic gas estimation."""
        priority_fee = self._w3.to_wei(0.1, "gwei")  # Reasonable priority fee for Base
        base_fee = max(self._w3.eth.gas_price, self._w3.to_wei(1, "gwei"))  # Floor: 1 gwei
        max_fee = max(base_fee * 2, priority_fee + base_fee)

        params = {
            "from": account.address,
            "nonce": self._w3.eth.get_transaction_count(account.address, "pending"),
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
                _log.warning("Gas estimation failed, using 500k fallback", exc_info=True)
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
    def operator_address(self) -> Optional[str]:
        return self._operator_account.address if self._operator_account else None

    @property
    def evaluator_address(self) -> Optional[str]:
        return self._evaluator_account.address if self._evaluator_account else None

    @property
    def auditor_address(self) -> Optional[str]:
        return self._auditor_account.address if self._auditor_account else None
