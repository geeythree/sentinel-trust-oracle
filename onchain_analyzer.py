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

            # 5. ENS reverse resolution (Ethereum identity signal)
            ens_name = self._resolve_ens_name(agent_wallet)
            if ens_name:
                _log.info("ENS resolved: %s → %s", agent_wallet, ens_name)

            # 6. Score (no penalty for new wallets; HHI sybil penalty applied if concentrated)
            score = self._compute_onchain_score(tx_count, balance_eth, existing_rep, has_code, ens_name)

            return OnchainAnalysis(
                success=True,
                wallet_address=agent_wallet,
                transaction_count=tx_count,
                balance_eth=balance_eth,
                has_contract_code=has_code,
                existing_reputation=existing_rep,
                onchain_score=score,
                ens_name=ens_name,
            )
        except Exception as e:
            _log.warning("On-chain analysis failed for %s: %s", agent_wallet, e, exc_info=True)
            ens_name = ""
            return OnchainAnalysis(
                success=False,
                wallet_address=agent_wallet,
                onchain_score=0,  # failure = no data = 0 score
            )

    def _resolve_ens_name(self, wallet_address: str) -> str:
        """Resolve wallet address to ENS name via public reverse lookup.
        Returns empty string if not found or on error."""
        import requests as _req
        try:
            resp = _req.get(
                f"https://api.ensideas.com/ens/resolve/{wallet_address}",
                timeout=5,
            )
            if resp.status_code == 200:
                name = resp.json().get("name") or ""
                if name and name.endswith(".eth"):
                    return name
        except Exception:
            pass
        return ""

    def _check_existing_reputation(self, agent_id: int) -> ExistingReputation:
        """Read from ERC-8004 Reputation Registry (with HHI sybil detection)."""
        try:
            count, avg_value, decimals, hhi, unique = self._blockchain.get_reputation_with_hhi(agent_id)
            return ExistingReputation(
                feedback_count=count,
                summary_value=avg_value,
                summary_decimals=decimals,
                hhi=hhi,
                unique_reviewer_count=unique,
            )
        except Exception:
            return ExistingReputation(feedback_count=0, summary_value=0, summary_decimals=0)

    def _compute_onchain_score(
        self,
        tx_count: int,
        balance: float,
        rep: ExistingReputation,
        has_code: bool = False,
        ens_name: str = "",
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

        # HHI sybil penalty — smooth continuous function (only when ≥3 feedbacks)
        # Starts at HHI=1000 (10 equal reviewers), reaches max -15 at HHI=10000 (single reviewer)
        # penalty = 15 × (HHI - 1000) / 9000, clamped to [0, 15]
        if rep.feedback_count >= 3 and rep.hhi > 1000:
            raw_penalty = 15.0 * (rep.hhi - 1000) / 9000.0
            penalty = round(raw_penalty)
            score -= penalty
            if penalty >= 8:
                _log.warning(
                    "Sybil flag: HHI=%d penalty=-%d (unique_reviewers=%d count=%d)",
                    rep.hhi, penalty, rep.unique_reviewer_count, rep.feedback_count,
                )
            else:
                _log.info(
                    "HHI concentration: HHI=%d penalty=-%d (unique=%d)",
                    rep.hhi, penalty, rep.unique_reviewer_count,
                )

        # Contract code detection (max +10)
        if has_code:
            score += 10  # address has deployed contract code — strong dev signal

        # ENS identity bonus (max +8): verified on-chain identity
        if ens_name:
            score += 8
            # Capped total stays at min(100, score)

        return min(100, max(0, score))
