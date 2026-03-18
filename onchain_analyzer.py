"""Onchain wallet analysis for agent trust evaluation."""
from __future__ import annotations

from web3 import Web3

from logger import AgentLogger
from models import ExistingReputation, OnchainAnalysis


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

            # 3. Check existing reputation in ERC-8004
            existing_rep = self._check_existing_reputation(agent_id)

            # 4. Score (no penalty for new wallets)
            score = self._compute_onchain_score(tx_count, balance_eth, existing_rep)

            return OnchainAnalysis(
                success=True,
                wallet_address=agent_wallet,
                transaction_count=tx_count,
                balance_eth=balance_eth,
                existing_reputation=existing_rep,
                onchain_score=score,
            )
        except Exception as e:
            return OnchainAnalysis(
                success=False,
                wallet_address=agent_wallet,
                onchain_score=50,  # neutral on failure
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
    ) -> int:
        """Score 0-100. Neutral for new wallets, bonus for established ones."""
        score = 50  # NEUTRAL baseline for new wallets

        # Transaction count bonus (no penalty below 5)
        if tx_count > 50:
            score += 30
        elif tx_count > 10:
            score += 20
        elif tx_count > 5:
            score += 10
        # 0-5 txs: stay at 50 (neutral)

        # Balance bonus (has gas money = probably real)
        if balance > 0.01:
            score += 10
        elif balance > 0.001:
            score += 5

        # Existing positive reputation bonus
        if rep.feedback_count > 0 and rep.summary_value > 0:
            score += 10

        return min(100, score)
