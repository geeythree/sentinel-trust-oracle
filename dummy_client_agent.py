"""Demo: A simple agent that calls Sentinel's MCP verify_agent tool.

Usage:
    python3 dummy_client_agent.py [--agent-id N]

This script spawns the Sentinel MCP server as a subprocess and calls
the verify_agent tool for a specific agent. Useful for demos showing
agent-to-agent trust verification.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Default agent to verify (override with --agent-id)
TARGET_AGENT_ID = 1
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


async def main():
    agent_id = TARGET_AGENT_ID
    if len(sys.argv) > 2 and sys.argv[1] == "--agent-id":
        agent_id = int(sys.argv[2])

    print(f"[DemoAgent] Starting Sentinel MCP client...")
    print(f"[DemoAgent] Target: Agent #{agent_id}")

    server_params = StdioServerParameters(
        command="python3",
        args=[os.path.join(_PROJECT_DIR, "mcp_server.py")],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # List available tools
            tools = await session.list_tools()
            print(f"[DemoAgent] Available tools: {[t.name for t in tools.tools]}")

            # First check existing reputation
            print(f"\n[DemoAgent] Checking existing reputation...")
            rep_result = await session.call_tool("check_reputation", {"agent_id": agent_id})
            rep_data = json.loads(rep_result.content[0].text)
            print(f"[DemoAgent] Reputation: {json.dumps(rep_data, indent=2)}")

            # Now verify the agent (full pipeline)
            print(f"\n[DemoAgent] Running full trust verification...")
            result = await session.call_tool("verify_agent", {"agent_id": agent_id})
            verdict = json.loads(result.content[0].text)

            print(f"\n{'='*50}")
            print(f"  TRUST VERDICT")
            print(f"{'='*50}")
            print(f"  Agent ID:         #{verdict['agent_id']}")
            print(f"  Verdict:          {verdict['verdict']}")
            print(f"  Trust Score:      {verdict['trust_score']}/100")
            print(f"  Confidence:       {verdict['confidence']}%")
            print(f"  Identity OK:      {verdict['identity_verified']}")
            print(f"  Endpoints:        {verdict['endpoints_live']}/{verdict['endpoints_declared']} live")
            print(f"  Anomalies:        {'Yes' if verdict['anomalies_detected'] else 'No'}")
            if verdict.get('attestation_uid'):
                print(f"  Attestation:      {verdict['attestation_uid']}")
            if verdict.get('basescan_url'):
                print(f"  BaseScan:         {verdict['basescan_url']}")
            print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())
