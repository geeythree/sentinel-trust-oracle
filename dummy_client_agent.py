"""Trust-gated agent interaction demo.

Shows the full trust verification flow:
  1. Agent A asks Sentinel about Agent B via MCP
  2. Sentinel returns trust verdict
  3. Agent A decides: proceed (score >= threshold) or refuse
  4. Decision is logged with on-chain proof

Usage:
    python3 dummy_client_agent.py --agent-id N [--threshold 70]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Defaults
TARGET_AGENT_ID = 1
TRUST_THRESHOLD = 70  # minimum score to proceed with interaction
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TOOL_CALL_TIMEOUT = 180  # seconds — Venice can take up to 120s


def _parse_args() -> tuple[int, int]:
    """Parse CLI arguments."""
    agent_id = TARGET_AGENT_ID
    threshold = TRUST_THRESHOLD
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--agent-id" and i + 1 < len(args):
            agent_id = int(args[i + 1])
            i += 2
        elif args[i] == "--threshold" and i + 1 < len(args):
            threshold = int(args[i + 1])
            i += 2
        else:
            i += 1
    return agent_id, threshold


async def main():
    agent_id, threshold = _parse_args()

    print("=" * 60)
    print("  TRUST-GATED AGENT INTERACTION DEMO")
    print("=" * 60)
    print(f"  Agent A (this client) wants to interact with Agent #{agent_id}")
    print(f"  Trust threshold: {threshold}/100")
    print(f"  Querying Sentinel Trust Oracle via MCP...")
    print("=" * 60)

    server_params = StdioServerParameters(
        command="python3",
        args=[os.path.join(_PROJECT_DIR, "mcp_server.py")],
    )

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                tool_names = [t.name for t in tools.tools]
                print(f"\n[Agent A] Connected to Sentinel. Tools: {tool_names}")

                # --- Step 1: Check existing trust chain ---
                print(f"\n[Step 1] Checking trust chain for Agent #{agent_id}...")
                trust_chain = None
                if "get_trust_chain" in tool_names:
                    try:
                        chain_result = await asyncio.wait_for(
                            session.call_tool("get_trust_chain", {"agent_id": agent_id}),
                            timeout=TOOL_CALL_TIMEOUT,
                        )
                        trust_chain = json.loads(chain_result.content[0].text)
                        if "error" not in trust_chain and trust_chain.get("trust_score") is not None:
                            print(f"  Cached trust data found:")
                            print(f"    Score: {trust_chain['trust_score']}/100")
                            print(f"    Confidence: {trust_chain.get('confidence', 'N/A')}%")
                            print(f"    State: {trust_chain.get('state', 'N/A')}")
                            if trust_chain.get("attestation_uid"):
                                print(f"    Attestation: {trust_chain['attestation_uid']}")
                        else:
                            trust_chain = None
                            print(f"  No cached trust data. Will run full verification.")
                    except Exception as e:
                        print(f"  Trust chain lookup failed: {e}")

                # --- Step 2: Full verification (if no cached data) ---
                if trust_chain is None:
                    print(f"\n[Step 2] Running full trust verification...")
                    try:
                        result = await asyncio.wait_for(
                            session.call_tool("verify_agent", {"agent_id": agent_id}),
                            timeout=TOOL_CALL_TIMEOUT,
                        )
                        if not result.content or not result.content[0].text:
                            print("[Agent A] ERROR: Empty response from Sentinel")
                            return

                        verdict = json.loads(result.content[0].text)
                        if "error" in verdict:
                            print(f"[Agent A] Verification failed: {verdict['error']}")
                            print(f"\n  DECISION: REFUSE interaction (verification error)")
                            return

                        trust_chain = {
                            "trust_score": verdict["trust_score"],
                            "confidence": verdict["confidence"],
                            "state": verdict["verdict"],
                            "attestation_uid": verdict.get("attestation_uid"),
                            "basescan_url": verdict.get("basescan_url"),
                            "identity_verified": verdict.get("identity_verified"),
                            "endpoints_live": verdict.get("endpoints_live"),
                            "endpoints_declared": verdict.get("endpoints_declared"),
                            "anomalies_detected": verdict.get("anomalies_detected"),
                        }
                    except asyncio.TimeoutError:
                        print(f"[Agent A] Verification timed out after {TOOL_CALL_TIMEOUT}s")
                        print(f"\n  DECISION: REFUSE interaction (timeout)")
                        return
                else:
                    print(f"\n[Step 2] Skipped (using cached trust data)")

                # --- Step 3: Trust-gated decision ---
                score = trust_chain.get("trust_score", 0)
                confidence = trust_chain.get("confidence", 0)

                print(f"\n{'=' * 60}")
                print(f"  TRUST VERDICT")
                print(f"{'=' * 60}")
                print(f"  Agent #{agent_id}:")
                print(f"    Trust Score:    {score}/100")
                print(f"    Confidence:     {confidence}%")
                print(f"    State:          {trust_chain.get('state', 'UNKNOWN')}")
                if trust_chain.get("identity_verified") is not None:
                    print(f"    Identity OK:    {trust_chain['identity_verified']}")
                if trust_chain.get("endpoints_live") is not None:
                    print(f"    Endpoints:      {trust_chain['endpoints_live']}/{trust_chain.get('endpoints_declared', '?')} live")
                if trust_chain.get("anomalies_detected") is not None:
                    print(f"    Anomalies:      {'Yes' if trust_chain['anomalies_detected'] else 'No'}")
                if trust_chain.get("attestation_uid"):
                    print(f"    Attestation:    {trust_chain['attestation_uid']}")
                if trust_chain.get("basescan_url"):
                    print(f"    BaseScan:       {trust_chain['basescan_url']}")

                print(f"\n  Threshold: {threshold}/100")
                print(f"{'=' * 60}")

                if score >= threshold:
                    print(f"\n  DECISION: PROCEED with interaction")
                    print(f"  Reason: Trust score {score} >= threshold {threshold}")
                    print(f"  Agent #{agent_id} is trusted. Agent A would now interact.")
                else:
                    print(f"\n  DECISION: REFUSE interaction")
                    print(f"  Reason: Trust score {score} < threshold {threshold}")
                    print(f"  Agent #{agent_id} did not meet trust requirements.")

                # --- Step 4: Log the decision ---
                print(f"\n[Step 4] Interaction decision recorded.")
                if trust_chain.get("attestation_uid"):
                    print(f"  On-chain proof: EAS attestation {trust_chain['attestation_uid']}")
                    print(f"  Any agent can independently verify this verdict on-chain.")

    except Exception as e:
        print(f"[Agent A] Failed to connect to Sentinel MCP server: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
