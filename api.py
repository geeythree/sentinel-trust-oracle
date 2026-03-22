"""FastAPI HTTP wrapper for Sentinel — Agent Trust Oracle.

Exposes the evaluation pipeline as REST endpoints for the interactive dashboard.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from web3 import Web3

import logging

_log = logging.getLogger("sentinel.api")

# x402 replay protection (in-memory; resets on server restart — acceptable for hackathon)
_used_payment_hashes: set[str] = set()

# USDC Transfer event topic
_USDC_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

app = FastAPI(title="Sentinel — Agent Trust Oracle", version="1.0.0")

# CORS: allow dashboard origins + any for hackathon demo convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# --- Lazy-init pipeline (same pattern as mcp_server.py) ---

_pipeline_cache: tuple | None = None
_eval_lock = asyncio.Lock()


def _get_pipeline():
    """Lazy-initialize the full Sentinel pipeline (cached after first call)."""
    global _pipeline_cache
    if _pipeline_cache is not None:
        return _pipeline_cache

    import config as config_module
    if config_module.config is None:
        config_module.config = config_module.create_config()
    from config import config  # noqa: F811

    from logger import AgentLogger
    from agent_discovery import AgentDiscovery
    from agent_verifier import AgentVerifier
    from liveness_checker import LivenessChecker
    from onchain_analyzer import OnchainAnalyzer
    from venice import VeniceClient
    from scorer import Scorer
    from blockchain import BlockchainClient
    from orchestrator import Orchestrator

    logger = AgentLogger(config.AGENT_LOG_PATH, budget=config.TOOL_CALL_BUDGET)
    blockchain = BlockchainClient(logger)
    discovery = AgentDiscovery(logger, blockchain)
    agent_verifier = AgentVerifier(logger)
    liveness_checker = LivenessChecker(logger)
    onchain_analyzer = OnchainAnalyzer(logger, blockchain)
    venice = VeniceClient(logger)
    scorer = Scorer()

    orchestrator = Orchestrator(
        logger, discovery, agent_verifier, liveness_checker,
        onchain_analyzer, venice, scorer, blockchain,
    )

    _pipeline_cache = (orchestrator, discovery, blockchain, config)
    return _pipeline_cache


def _verify_usdc_payment(tx_hash: str, blockchain, fee_usdc: int) -> bool:
    """Verify a USDC payment by checking the tx receipt for a Transfer event.

    Looks for Transfer(from, to=OPERATOR, value>=fee_usdc) in the tx receipt logs.
    """
    if not tx_hash or not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return False
    if tx_hash in _used_payment_hashes:
        _log.warning("x402 replay attempt: tx_hash=%s already used", tx_hash)
        return False
    try:
        receipt = blockchain.w3.eth.get_transaction_receipt(tx_hash)
        if not receipt or receipt.status != 1:
            return False

        import config as cm
        operator = blockchain._operator_account.address.lower()

        for log_entry in receipt.logs:
            topics = log_entry.get("topics", [])
            if not topics:
                continue
            topic0 = topics[0].hex() if isinstance(topics[0], bytes) else str(topics[0])
            if topic0.lower() != _USDC_TRANSFER_TOPIC:
                continue
            if len(topics) < 3:
                continue
            # topics[2] = 'to' address (indexed, padded to 32 bytes)
            to_bytes = topics[2]
            to_addr = "0x" + (to_bytes.hex() if isinstance(to_bytes, bytes) else str(to_bytes))[-40:]
            if to_addr.lower() != operator:
                continue
            # data = value (uint256)
            data = log_entry.get("data", b"")
            if isinstance(data, bytes):
                value = int.from_bytes(data[:32], "big") if len(data) >= 32 else 0
            else:
                value = int(str(data), 16) if data else 0
            if value >= fee_usdc:
                _used_payment_hashes.add(tx_hash)
                _log.info("x402 payment verified: tx=%s amount=%d operator=%s", tx_hash, value, operator)
                return True

        return False
    except Exception:
        _log.warning("x402 payment verification failed for tx=%s", tx_hash, exc_info=True)
        return False


# --- Request model ---

class EvaluateRequest(BaseModel):
    agent_id: int


# --- Endpoints ---

DASHBOARD_DIR = Path(__file__).parent / "dashboard"


@app.get("/")
async def serve_dashboard():
    """Serve the interactive dashboard."""
    index = DASHBOARD_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return FileResponse(index, media_type="text/html")


@app.get("/api/health")
async def health():
    """Health check — confirms API is running."""
    try:
        _, _, _, cfg = _get_pipeline()
        return {
            "status": "ok",
            "network": "Base Sepolia" if cfg.USE_TESTNET else "Base Mainnet",
        }
    except Exception:
        return {"status": "degraded", "note": "Pipeline not yet initialized"}


@app.post("/api/evaluate")
async def evaluate(req: EvaluateRequest, x_payment: Optional[str] = Header(None, alias="X-PAYMENT")):
    """Run full evaluation pipeline for an agent. Requires 0.01 USDC payment on Base (x402).

    Payment flow:
    1. POST without X-PAYMENT → 402 with payment requirements
    2. Pay USDC to the listed address on Base mainnet
    3. Retry with X-PAYMENT: base64({"txHash":"0x...","scheme":"exact","network":"base"})
    """
    if req.agent_id < 0:
        raise HTTPException(status_code=400, detail="agent_id must be non-negative")

    _, _, blockchain, cfg = _get_pipeline()

    # x402 payment gate (mainnet only — skip on testnet for development)
    if cfg.X402_ENABLED and not cfg.USE_TESTNET:
        operator_address = blockchain._operator_account.address
        usdc_address = cfg.USDC_BASE_MAINNET

        if not x_payment:
            return JSONResponse(
                status_code=402,
                content={
                    "x402Version": 1,
                    "accepts": [{
                        "scheme": "exact",
                        "network": "base",
                        "maxAmountRequired": str(cfg.EVALUATION_FEE_USDC),
                        "resource": "/api/evaluate",
                        "description": "Sentinel trust evaluation — 0.01 USDC",
                        "mimeType": "application/json",
                        "payTo": operator_address,
                        "maxTimeoutSeconds": 300,
                        "asset": usdc_address,
                        "extra": {"name": "USDC", "decimals": 6},
                    }],
                    "error": "X-PAYMENT header required. Pay 0.01 USDC to the listed address on Base.",
                },
            )

        # Verify payment
        try:
            payment_data = json.loads(base64.b64decode(x_payment.encode()).decode())
            tx_hash = payment_data.get("txHash", "")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid X-PAYMENT header: must be base64-encoded JSON with txHash")

        loop = asyncio.get_running_loop()
        payment_valid = await loop.run_in_executor(
            None, _verify_usdc_payment, tx_hash, blockchain, cfg.EVALUATION_FEE_USDC
        )
        if not payment_valid:
            raise HTTPException(status_code=402, detail="Payment verification failed. Ensure tx is confirmed on Base mainnet with correct USDC amount.")

    if _eval_lock.locked():
        raise HTTPException(
            status_code=429,
            detail="An evaluation is already in progress. Please wait.",
        )

    async with _eval_lock:
        loop = asyncio.get_running_loop()
        orchestrator, discovery, blockchain, cfg = _get_pipeline()

        try:
            agent = await asyncio.wait_for(
                loop.run_in_executor(
                    None, discovery.discover_agent_by_id, req.agent_id
                ),
                timeout=120,
            )
            verdict = await asyncio.wait_for(
                loop.run_in_executor(
                    None, orchestrator.evaluate_single, agent
                ),
                timeout=300,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Evaluation timed out. Try again.")
        except Exception as e:
            _log.exception("Evaluation failed for agent %d", req.agent_id)
            raise HTTPException(status_code=500, detail="Evaluation failed. Please try again.")

        result = verdict.to_dashboard_dict()

        # Auto-append to dashboard/results.json
        try:
            await loop.run_in_executor(
                None, orchestrator._update_dashboard, [verdict]
            )
        except Exception:
            _log.debug("Dashboard update skipped", exc_info=True)

        return result


@app.get("/api/reputation/{agent_id}")
async def get_reputation(agent_id: int):
    """Read on-chain reputation for an agent."""
    loop = asyncio.get_running_loop()
    _, _, blockchain, _ = _get_pipeline()

    try:
        count, value, decimals, hhi, unique = await loop.run_in_executor(
            None, blockchain.get_reputation_with_hhi, agent_id
        )
        return {
            "agent_id": agent_id,
            "feedback_count": count,
            "summary_value": value,
            "summary_decimals": decimals,
            "unique_reviewer_count": unique,
            "hhi": hhi,
            "concentration": (
                "HIGH_CONCENTRATION" if hhi > 2500 else
                "MODERATE_CONCENTRATION" if hhi > 1500 else
                "HEALTHY"
            ),
        }
    except Exception:
        _log.exception("Reputation lookup failed for agent %d", agent_id)
        raise HTTPException(status_code=500, detail="Reputation lookup failed.")


@app.get("/api/trust-chain/{agent_id}")
async def get_trust_chain(agent_id: int):
    """Get an agent's trust chain: score, confidence, timestamp, attestation UID."""
    results_path = DASHBOARD_DIR / "results.json"
    if results_path.exists():
        try:
            with open(results_path) as f:
                results = json.load(f)
            for r in results:
                if r.get("agent_id") == agent_id:
                    return {
                        "agent_id": agent_id,
                        "trust_score": r.get("composite_score"),
                        "confidence": r.get("confidence"),
                        "timestamp": r.get("timestamp"),
                        "attestation_uid": r.get("attestation_uid"),
                        "tx_hash": r.get("tx_hash"),
                        "dimensions": r.get("dimensions"),
                        "input_hash": r.get("input_hash"),
                        "chain_id": r.get("chain_id"),
                        "state": r.get("state"),
                    }
        except (json.JSONDecodeError, ValueError):
            pass
    raise HTTPException(status_code=404, detail=f"No evaluation found for agent {agent_id}")


class TransitiveTrustRequest(BaseModel):
    requester_agent_id: int
    target_agent_id: int


@app.post("/api/transitive-trust")
async def compute_transitive_trust(req: TransitiveTrustRequest):
    """Compute transitive trust: requester_confidence * target_score / 100."""
    results_path = DASHBOARD_DIR / "results.json"
    req_data = tgt_data = None
    if results_path.exists():
        try:
            with open(results_path) as f:
                results = json.load(f)
            for r in results:
                if r.get("agent_id") == req.requester_agent_id:
                    req_data = r
                if r.get("agent_id") == req.target_agent_id:
                    tgt_data = r
        except (json.JSONDecodeError, ValueError):
            pass

    if req_data is None:
        raise HTTPException(status_code=404, detail=f"No evaluation found for requester agent {req.requester_agent_id}")
    if tgt_data is None:
        raise HTTPException(status_code=404, detail=f"No evaluation found for target agent {req.target_agent_id}")

    # Subjective Logic discount operator (Jøsang 2016)
    req_score = (req_data.get("composite_score") or 0) / 100.0
    req_uncertainty = 1.0 - (req_data.get("confidence") or 0) / 100.0
    req_disbelief = max(0.0, 1.0 - req_score - req_uncertainty)

    tgt_score_raw = (tgt_data.get("composite_score") or 0) / 100.0
    tgt_uncertainty = 1.0 - (tgt_data.get("confidence") or 0) / 100.0
    tgt_disbelief = max(0.0, 1.0 - tgt_score_raw - tgt_uncertainty)

    b_derived = req_score * tgt_score_raw
    d_derived = req_score * tgt_disbelief
    u_derived = req_disbelief + req_uncertainty + req_score * tgt_uncertainty
    total = b_derived + d_derived + u_derived
    if total > 0:
        b_derived /= total
        d_derived /= total
        u_derived /= total

    projected = b_derived + 0.5 * u_derived
    derived_trust = round(projected * 100, 1)
    derived_confidence = round((1.0 - u_derived) * 100, 1)

    return {
        "requester_agent_id": req.requester_agent_id,
        "requester_score": req_data.get("composite_score"),
        "requester_confidence": req_data.get("confidence"),
        "target_agent_id": req.target_agent_id,
        "target_score": tgt_data.get("composite_score"),
        "target_confidence": tgt_data.get("confidence"),
        "derived_trust": derived_trust,
        "derived_confidence": derived_confidence,
        "model": "subjective_logic_discount",
        "interpretation": (
            "TRUSTED" if derived_trust >= 70 else
            "MODERATE" if derived_trust >= 50 else
            "LOW_TRUST"
        ),
        "note": (
            f"Trust decays through the referral chain. "
            f"Uncertainty u={u_derived:.3f} grows with each hop."
        ),
    }


@app.get("/api/results")
async def get_results():
    """Return current results.json contents."""
    results_path = DASHBOARD_DIR / "results.json"
    if not results_path.exists():
        return []
    try:
        with open(results_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return []


# Serve results.json as static file at /results.json (for dashboard fetch)
@app.get("/results.json")
async def serve_results_json():
    """Serve results.json for the dashboard's fetch('results.json') call."""
    results_path = DASHBOARD_DIR / "results.json"
    if not results_path.exists():
        return JSONResponse(content=[])
    return FileResponse(results_path, media_type="application/json")


# Serve agent.json for wallet info display
@app.get("/agent.json")
async def serve_agent_json():
    """Serve agent.json for dashboard wallet info."""
    agent_path = Path(__file__).parent / "agent.json"
    if not agent_path.exists():
        raise HTTPException(status_code=404, detail="agent.json not found")
    return FileResponse(agent_path, media_type="application/json")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
