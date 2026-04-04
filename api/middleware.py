# api/middleware.py
"""x402 payment gate, caching, CORS setup."""
from __future__ import annotations
import os
import time
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_cache: dict[str, Any] = {}
_cache_ts: float = 0
CACHE_TTL = int(os.getenv("CACHE_TTL_SEC", "300"))


def get_cached_signals() -> Optional[dict]:
    if time.time() - _cache_ts < CACHE_TTL and _cache:
        return _cache
    return None


def set_cached_signals(signals: dict):
    global _cache, _cache_ts
    _cache = signals
    _cache_ts = time.time()


def setup_x402(app):
    pay_to = os.getenv("PAY_TO", "")
    if not pay_to:
        logger.info("x402 disabled — PAY_TO not set")
        return
    try:
        from x402.http import PaymentMiddlewareASGI
        from x402.types.evm import ExactEvmServerScheme
        price = os.getenv("SIGNAL_PRICE_USDC", "0.001")
        facilitator_url = os.getenv("X402_FACILITATOR_URL",
                                     "https://api.cdp.coinbase.com/platform/v2/x402")
        scheme = ExactEvmServerScheme()
        routes = [
            {"path": "/signal", "method": "GET",
             "price": {"amount": price, "currency": "USDC",
                       "network": "eip155:8453",
                       "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
             "payTo": pay_to},
            {"path": "/signal/*", "method": "GET",
             "price": {"amount": price, "currency": "USDC",
                       "network": "eip155:8453",
                       "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
             "payTo": pay_to},
            {"path": "/performance/reputation", "method": "GET",
             "price": {"amount": price, "currency": "USDC",
                       "network": "eip155:8453",
                       "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
             "payTo": pay_to},
        ]
        app.add_middleware(PaymentMiddlewareASGI, scheme=scheme,
                          routes=routes, facilitator_url=facilitator_url)
        logger.info("x402 payment gate enabled")
    except Exception as e:
        logger.warning(f"x402 setup failed: {e}")


def setup_cors(app):
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE",
                        "X-PAYMENT", "X-PAYMENT-RESPONSE"],
    )
