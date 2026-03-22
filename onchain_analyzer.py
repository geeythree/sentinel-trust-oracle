"""Onchain wallet analysis for agent trust evaluation."""
from __future__ import annotations

import logging

from web3 import Web3

from logger import AgentLogger
from models import ExistingReputation, OnchainAnalysis

_log = logging.getLogger(__name__)


class OnchainAnalyzer:
    """Analyze the agent's wallet onchain history. No penalty for new wallets."""

    def __init__(self, logger: AgentLogger, blockchain) -> None:
        self._logger = logger
        self._blockchain = blockchain

    def analyze(self, agent_wallet: str, agent_id: int) -> OnchainAnalysis:
        """Analyze wallet history and existing reputation."""
        w3 = self._blockchain.w3

        try:
            # 1. Transaction count
            tx_count = w3.eth.get_transaction_count(
                Web3.to_checksum_address(agent_wallet)
            )

            # 2. ETH balance
            balance_wei = w3.eth.get_balance(
                Web3.to_checksum_address(agent_wallet)
            )
            balance_eth = float(Web3.from_wei(balance_wei, "ether"))

            # 3. Check if address has deployed code (contract)
            has_code = len(w3.eth.get_code(Web3.to_checksum_address(agent_wallet))) > 0

            # 4. Check existing reputation in ERC-8004
            existing_rep = self._check_existing_reputation(agent_id)

            # 5. Score (no penalty for new wallets)
            score = self._compute_onchain_score(tx_count, balance_eth, existing_rep, has_code)

            return OnchainAnalysis(
                success=True,
                wallet_address=agent_wallet,
                transaction_count=tx_count,
                balance_eth=balance_eth,
                has_contract_code=has_code,
                existing_reputation=existing_rep,
                onchain_score=score,
            )
        except Exception as e:
            _log.warning("On-chain analysis failed for %s: %s", agent_wallet, e, exc_info=True)
            return OnchainAnalysis(
                success=False,
                wallet_address=agent_wallet,
                onchain_score=0,  # failure = no data = 0 score
            )

    def _check_existing_reputation(self, agent_id: int) -> ExistingReputation:
        """Read from ERC-8004 Reputation Registry."""
        try:
            result = self._blockchain.get_reputation_summary(agent_id)
            return ExistingReputation(
                feedback_count=result[0],
                summary_value=result[1],
                summary_decimals=result[2],
            )
        except Exception:
            return ExistingReputation(feedback_count=0, summary_value=0, summary_decimals=0)

    def _compute_onchain_score(
        self,
        tx_count: int,
        balance: float,
        rep: ExistingReputation,
        has_code: bool = False,
    ) -> int:
        """Score 0-100. Finer granularity for better score discrimination.

        Components:
        - Transaction count: 0-25 (7 buckets)
        - Balance: 0-12 (5 tiers)
        - Reputation: 0-13 (3 tiers)
        - Contract code: 0-10 (binary)
        - Baseline: 50 for successful analysis

        Total range: 50-100 for active wallets, 50 for empty new wallets.
        Clamped to min(100, score).
        """
        score = 50  # baseline for successful analysis

        # Transaction count (7 buckets, max +25)
        if tx_count > 100:
            score += 25
        elif tx_count > 50:
            score += 22
        elif tx_count > 20:
            score += 18
        elif tx_count > 10:
            score += 14
        elif tx_count > 5:
            score += 10
        elif tx_count > 2:
            score += 6
        elif tx_count > 0:
            score += 3
        # 0 txs: +0

        # Balance gradient (max +12)
        if balance > 0.1:
            score += 12
        elif balance > 0.01:
            score += 8
        elif balance > 0.001:
            score += 4
        elif balance > 0:
            score += 2
        # 0 balance: +0

        # Existing reputation (max +13)
        if rep.feedback_count > 5 and rep.summary_value > 0:
            score += 13
        elif rep.feedback_count > 0 and rep.summary_value > 0:
            score += 8
        elif rep.feedback_count > 0:
            score += 3
        # no reputation: +0

        # Contract code detection (max +10)
        if has_code:
            score += 10  # address has deployed contract code — strong dev signal

        return min(100, score)
