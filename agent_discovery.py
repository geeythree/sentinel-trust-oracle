"""Agent discovery via ERC-8004 Identity Registry events."""
from __future__ import annotations

from typing import Optional

from web3 import Web3

from config import config
from exceptions import DiscoveryError
from logger import AgentLogger
from models import DiscoveredAgent


class AgentDiscovery:
    """Find newly registered agents on the ERC-8004 Identity Registry."""

    def __init__(self, logger: AgentLogger, blockchain) -> None:
        self._logger = logger
        self._blockchain = blockchain

    def discover_agents(
        self,
        from_block: int,
        to_block: int,
        max_agents: int = 10,
    ) -> list[DiscoveredAgent]:
        """Query ERC-8004 Identity Registry for Registered events.

        NOTE (production): eth_getLogs is O(n blocks) and times out on long ranges.
        For scalable historical discovery, use The Graph subgraph instead:
          https://thegraph.com/docs/en/querying/querying-from-an-application/
        Once a subgraph is deployed for ERC-8004, replace get_registered_events()
        with a GraphQL query to instantly retrieve all Registered events.
        """
        try:
            entries = self._blockchain.get_registered_events(from_block, to_block)
        except Exception as e:
            raise DiscoveryError(f"Failed to query Registered events: {e}") from e

        agents = []
        for entry in entries[:max_agents]:
            args = entry.get("args", {})
            agent_id = args.get("agentId", 0)
            agent_uri = args.get("agentURI", "") or args.get("uri", "")
            owner = args.get("owner", "")

            if not agent_id:
                continue

            agents.append(DiscoveredAgent(
                agent_id=agent_id,
                agent_uri=agent_uri,
                owner_address=owner,
                chain_id=config.CHAIN_ID if not config.USE_TESTNET else 84532,
                block_number=entry.get("blockNumber"),
                discovery_source="erc8004_events",
            ))

        return agents

    def discover_agent_by_id(self, agent_id: int) -> DiscoveredAgent:
        """Manual mode: look up by ERC-8004 agent ID (tokenId)."""
        try:
            uri, owner = self._blockchain.get_agent_by_id(agent_id)
        except Exception as e:
            raise DiscoveryError(f"Failed to look up agent #{agent_id}: {e}") from e

        return DiscoveredAgent(
            agent_id=agent_id,
            agent_uri=uri,
            owner_address=owner,
            chain_id=config.CHAIN_ID if not config.USE_TESTNET else 84532,
            discovery_source="manual_id",
        )

    def discover_agent_by_address(self, address: str) -> DiscoveredAgent:
        """Manual mode: look up a specific agent by wallet address.

        Iterates recent Registered events to find an agent owned by this address.
        """
        latest_block = self._blockchain.get_latest_block()
        from_block = max(0, latest_block - config.DISCOVERY_BLOCK_RANGE)

        try:
            entries = self._blockchain.get_registered_events(from_block, latest_block)
        except Exception as e:
            raise DiscoveryError(f"Failed to query events for address {address}: {e}") from e

        checksum_addr = Web3.to_checksum_address(address)
        for entry in entries:
            args = entry.get("args", {})
            owner = args.get("owner", "")
            if not owner:
                continue
            if Web3.to_checksum_address(owner) == checksum_addr:
                return DiscoveredAgent(
                    agent_id=args.get("agentId", 0),
                    agent_uri=args.get("agentURI", "") or args.get("uri", ""),
                    owner_address=owner,
                    chain_id=config.CHAIN_ID if not config.USE_TESTNET else 84532,
                    block_number=entry.get("blockNumber"),
                    discovery_source="manual_address",
                )

        raise DiscoveryError(f"No agent found for address {address} in last {config.DISCOVERY_BLOCK_RANGE} blocks")
