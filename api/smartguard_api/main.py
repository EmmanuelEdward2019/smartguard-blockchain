"""The application and intelligence layer — Section 3.7.

FastAPI application exposing the endpoints the presentation layer expects. The
model is loaded once at startup and held on app state; deserialising a 200-tree
forest per request would not fit inside NFR-01's two-second budget.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from . import chain_sync
from .chain import get_chain
from .config import get_settings
from .inference import RiskScorer
from .routers import admin, auth, escrows, health, risk

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("smartguard")

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    scorer = RiskScorer(
        model_path=settings.model_path,
        model_version=settings.model_version,
        budget_ms=settings.inference_budget_ms,
    )
    scorer.load()
    app.state.scorer = scorer

    if not scorer.is_available:
        # Not fatal. The service still serves reads and the health endpoint
        # reports `degraded`, which is more useful than refusing to start —
        # but no escrow can be admitted while this holds.
        logger.warning(
            "starting without a risk model; escrow initiation will fail closed",
        )

    # The oracle and integration layer. Reported at startup either way: a
    # service that silently runs without a chain looks identical to one whose
    # RPC endpoint is misconfigured, and the difference matters.
    chain = get_chain(settings)
    status = chain.status()
    if chain.available:
        logger.info(
            "chain layer ready: chain %s at block %s, escrow %s, operator %s",
            status.chain_id, status.block_number,
            status.escrow_contract, status.operator,
        )
        if chain_sync.start_listener(settings):
            logger.info("chain listener polling every %ss", settings.chain_poll_seconds)
    else:
        logger.warning("chain layer unavailable: %s", status.reason)

    try:
        yield
    finally:
        chain_sync.stop_listener()


app = FastAPI(
    title="SmartGuard API",
    description=(
        "Application and intelligence layer of the blockchain-enhanced, "
        "AI-powered escrow prototype."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def record_latency(request: Request, call_next):
    """Server-side timing on every response.

    Correction 17 found the latency evidence unusable because the measurement
    conditions were not retained. A header on every response means the figure is
    always available to a load test rather than reconstructed afterwards.
    """
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
    return response


for router in (health.router, auth.router, risk.router, escrows.router, admin.router):
    app.include_router(router, prefix=API_PREFIX)


@app.get("/")
def root() -> dict:
    return {
        "service": "SmartGuard API",
        "version": app.version,
        "layer": "application and intelligence",
        "docs": "/docs",
        "health": f"{API_PREFIX}/health",
    }
