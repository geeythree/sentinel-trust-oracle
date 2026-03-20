"""FastAPI HTTP wrapper for Sentinel — Agent Trust Oracle.

Exposes the evaluation pipeline as REST endpoints for the interactive dashboard.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import logging

_log = logging.getLogger("sentinel.api")

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
        return {"status": "ok", "note": "Pipeline not yet initialized"}


@app.post("/api/evaluate")
async def evaluate(req: EvaluateRequest):
    """Run full evaluation pipeline for an agent. One at a time."""
    if req.agent_id < 0:
        raise HTTPException(status_code=400, detail="agent_id must be non-negative")

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
        count, value, decimals = await loop.run_in_executor(
            None, blockchain.get_reputation_summary, agent_id
        )
        return {
            "agent_id": agent_id,
            "feedback_count": count,
            "summary_value": value,
            "summary_decimals": decimals,
        }
    except Exception:
        _log.exception("Reputation lookup failed for agent %d", agent_id)
        raise HTTPException(status_code=500, detail="Reputation lookup failed.")


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
