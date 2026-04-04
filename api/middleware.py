# api/middleware.py
"""x402 payment gate, caching, CORS, usage tracking middleware."""
from __future__ import annotations
import hashlib
import os
import time
import logging
import traceback
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)

_cache: dict[str, Any] = {}
_cache_ts: float = 0
CACHE_TTL = int(os.getenv("CACHE_TTL_SEC", "300"))

# Storage reference — set by setup_usage_tracking()
_tracking_storage = None


def get_cached_signals() -> Optional[dict]:
    if time.time() - _cache_ts < CACHE_TTL and _cache:
        return _cache
    return None


def set_cached_signals(signals: dict):
    global _cache, _cache_ts
    _cache = signals
    _cache_ts = time.time()


# ---------------------------------------------------------------------------
# User-Agent Classification
# ---------------------------------------------------------------------------

_AI_AGENT_SIGNATURES = {
    "claude": "claude", "anthropic": "claude", "claudebot": "claude",
    "gptbot": "openai", "chatgpt": "openai", "openai": "openai",
    "gemini": "google", "google-extended": "google",
    "langchain": "langchain", "crewai": "crewai", "autogpt": "autogpt",
    "mcp": "mcp_client",
}

_BOT_SIGNATURES = ("bot", "spider", "crawler", "bytespider", "amazonbot",
                    "ccbot", "facebookexternalhit", "twitterbot", "slurp")


def classify_user_agent(ua: str) -> str:
    """Classify user-agent into a category for analytics."""
    if not ua:
        return "unknown"
    ua_lower = ua.lower()

    # AI agents first (highest priority)
    for sig, label in _AI_AGENT_SIGNATURES.items():
        if sig in ua_lower:
            return label

    # Browsers
    if any(b in ua_lower for b in ("mozilla", "chrome", "safari", "firefox", "edge")):
        return "browser"

    # Dev tools
    if any(t in ua_lower for t in ("curl", "httpie", "postman", "insomnia", "wget")):
        return "dev_tool"

    # Generic bots
    if any(b in ua_lower for b in _BOT_SIGNATURES):
        return "bot"

    return "other"


# ---------------------------------------------------------------------------
# Request source classification
# ---------------------------------------------------------------------------

_OWN_HOSTS = {"web3-signals-api-production.up.railway.app", "localhost", "127.0.0.1"}
_INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")

_REFERER_MAP = {
    "mcp.so": "mcp.so", "pulsemcp": "pulsemcp", "mcpservers.org": "mcpservers.org",
    "x402list.fun": "x402list.fun", "glama.ai": "glama.ai", "smithery.ai": "smithery.ai",
    "google.com": "google", "bing.com": "bing", "twitter.com": "twitter", "x.com": "twitter",
    "reddit.com": "reddit", "github.com": "github", "linkedin.com": "linkedin",
}


def _get_real_ip(request: Request) -> str:
    """Extract real client IP from reverse proxy headers."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip", "")
    if xri:
        return xri.strip()
    return request.client.host if request.client else ""


def _classify_request_source(request: Request) -> str:
    """Classify request as internal, external, or unknown."""
    if _INTERNAL_API_KEY:
        if request.headers.get("x-internal-key", "") == _INTERNAL_API_KEY:
            return "internal"

    referer = (request.headers.get("referer", "") or "").lower()
    if any(host in referer for host in _OWN_HOSTS):
        return "internal"

    path = request.url.path
    if path in ("/dashboard", "/health") or path.startswith("/analytics") or path.startswith("/admin"):
        return "internal"
    if path.startswith("/api/"):
        return "internal"

    ua = (request.headers.get("user-agent", "") or "").lower()
    has_payment = bool(request.headers.get("payment-signature", "") or request.headers.get("x-payment", ""))

    if not has_payment and any(t in ua for t in ("postman", "curl", "httpie", "insomnia")):
        return "internal"

    ai_sigs = ("claudebot", "claude-web", "anthropic", "gptbot", "chatgpt", "openai",
               "mcp", "langchain", "crewai", "autogpt")
    if any(sig in ua for sig in ai_sigs):
        return "external"
    if has_payment:
        return "external"

    return "unknown"


def _classify_referer_source(referer: str) -> str:
    """Classify referer into a known source for attribution."""
    if not referer:
        return "direct"
    ref_lower = referer.lower()
    if any(host in ref_lower for host in _OWN_HOSTS):
        return "self"
    for pattern, label in _REFERER_MAP.items():
        if pattern in ref_lower:
            return label
    return "other"


def _make_fingerprint(ip: str, ua: str) -> str:
    """Short hash fingerprint from IP + user-agent."""
    return hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Usage Tracking Middleware
# ---------------------------------------------------------------------------

class UsageTrackingMiddleware(BaseHTTPMiddleware):
    """Logs every API request for analytics — user-agent, endpoint, duration, payment status."""

    SKIP_PATHS = {"/favicon.ico", "/openapi.json"}
    PAID_PATHS = {"/signal", "/performance/reputation"}

    def _is_paid_path(self, path: str) -> bool:
        if path in self.PAID_PATHS:
            return True
        if path.startswith("/signal/") and not path.endswith("/trace"):
            return True
        return False

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration_ms = (time.time() - start) * 1000

        path = request.url.path
        if path in self.SKIP_PATHS:
            return response

        # Detect x402 payment context
        is_paid_route = self._is_paid_path(path)
        has_payment_header = bool(
            request.headers.get("payment-signature", "")
            or request.headers.get("x-payment", "")
        )
        status = response.status_code

        if is_paid_route and status == 402:
            payment_status = "payment_required"
        elif is_paid_route and has_payment_header and status == 200:
            payment_status = "paid"
        elif is_paid_route and has_payment_header and status != 200:
            payment_status = "payment_failed"
        elif is_paid_route and not has_payment_header and status == 200:
            payment_status = "free"
        else:
            payment_status = None

        # Fire-and-forget logging
        try:
            if _tracking_storage:
                ua = request.headers.get("user-agent", "")
                client_ip = _get_real_ip(request)
                request_source = _classify_request_source(request)
                _tracking_storage.save_api_request(
                    endpoint=path,
                    method=request.method,
                    user_agent=ua,
                    status_code=status,
                    duration_ms=round(duration_ms, 1),
                    client_ip=client_ip,
                    payment_status=payment_status,
                    request_source=request_source,
                )

                if status >= 500:
                    _tracking_storage.save_error_event(
                        error_type="api_5xx",
                        source=path,
                        message=f"HTTP {status} on {request.method} {path}",
                    )
                if payment_status == "payment_failed":
                    _tracking_storage.save_error_event(
                        error_type="payment_failure",
                        source=path,
                        message=f"x402 payment failed on {path}",
                    )
        except Exception:
            logger.warning("Usage tracking failed for %s %s: %s",
                           request.method, path, traceback.format_exc(limit=1))

        return response


# ---------------------------------------------------------------------------
# Proxy Scheme Middleware (Railway HTTPS fix)
# ---------------------------------------------------------------------------

class ProxySchemeMiddleware(BaseHTTPMiddleware):
    """Rewrite request scheme to HTTPS when behind a reverse proxy."""
    async def dispatch(self, request, call_next):
        if request.headers.get("x-forwarded-proto") == "https":
            request.scope["scheme"] = "https"
        return await call_next(request)


# ---------------------------------------------------------------------------
# Setup functions
# ---------------------------------------------------------------------------

def setup_usage_tracking_storage(storage):
    """Set the storage backend for usage tracking. Called during app lifespan startup."""
    global _tracking_storage
    _tracking_storage = storage
    logger.info("Usage tracking storage connected")


def setup_proxy_scheme(app):
    """Add ProxySchemeMiddleware for Railway HTTPS rewrite."""
    app.add_middleware(ProxySchemeMiddleware)


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
