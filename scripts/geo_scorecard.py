#!/usr/bin/env python3
"""
GEO Scorecard — weekly generative-SEO health check for Web3 Signals.

Measures the four GEO layers from data we already have:
  1. AI-crawler traffic (training-corpus layer)   — own /analytics
  2. Agent/MCP consumption (agent-native layer)   — own /analytics/agents
  3. Coinbase Bazaar indexing + quality metrics   — CDP discovery API
  4. Directory listing status                     — HTTP checks

Usage:
    python3 scripts/geo_scorecard.py            # print markdown report
    python3 scripts/geo_scorecard.py --json     # machine-readable

Run weekly (cron/scheduled task) and diff week-over-week.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone

BASE = "https://web3-signals-api-production.up.railway.app"
BAZAAR = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"

# Listing pages to health-check (add new ones after each submission)
LISTINGS = {
    "mcp_registry": "https://registry.modelcontextprotocol.io/v0/servers?search=web3-signals",
    "glama": "https://glama.ai/mcp/servers/manavaga/web3-signals-mcp",
    "github": "https://github.com/manavaga/web3-signals-mcp",
}

# User-agent substrings that indicate AI-ecosystem traffic
AI_CRAWLERS = ["claudebot", "gptbot", "perplexity", "anthropic", "openai",
               "claude-code", "opencode", "mcp", "ccbot", "bytespider"]


def _get(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "geo-scorecard/1.0 (web3-signals self-check)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _get_json(url: str, timeout: int = 20):
    status, body = _get(url, timeout)
    return json.loads(body)


def collect() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    out = {"generated_at": now, "layers": {}}

    # Layer 1+2: own analytics
    try:
        a = _get_json(f"{BASE}/analytics")
        ua_types = a.get("by_client_type", {})
        top_uas = a.get("top_user_agents", [])
        ai_hits = sum(u.get("requests", 0) for u in top_uas
                      if any(sig in (u.get("user_agent") or "").lower() for sig in AI_CRAWLERS))
        out["layers"]["traffic"] = {
            "total_requests_7d": a.get("total_requests"),
            "unique_clients_7d": a.get("unique_clients"),
            "by_client_type": ua_types,
            "ai_crawler_hits_in_top_uas": ai_hits,
        }
    except Exception as e:
        out["layers"]["traffic"] = {"error": str(e)}

    try:
        x = _get_json(f"{BASE}/analytics/x402")
        out["layers"]["payments"] = {
            "external_paid_calls": x.get("external_paid_calls"),
            "internal_paid_calls": x.get("internal_paid_calls"),
            "challenges_402": x.get("total_402_challenges"),
            "conversion_rate_pct": x.get("conversion_rate_pct"),
        }
    except Exception as e:
        out["layers"]["payments"] = {"error": str(e)}

    # Layer 3: Bazaar indexing — offset pagination ({limit, offset, total})
    try:
        ours, offset, total, pages = [], 0, None, 0
        while total is None or offset < total:
            data = _get_json(f"{BAZAAR}?limit=100&offset={offset}", timeout=30)
            for item in data.get("items", []):
                if "web3-signals-api-production" in json.dumps(item):
                    ours.append({
                        "resource": item.get("resource"),
                        "lastUpdated": item.get("lastUpdated"),
                        "quality": item.get("quality"),
                    })
            pg = data.get("pagination", {})
            total = pg.get("total", 0)
            offset += pg.get("limit", 100) or 100
            pages += 1
            if pages > 300:
                break
        out["layers"]["bazaar"] = {"indexed_resources": ours, "total_bazaar_resources": total}
    except Exception as e:
        out["layers"]["bazaar"] = {"error": str(e)}

    # Layer 4: listing health checks
    listings = {}
    for name, url in LISTINGS.items():
        try:
            status, body = _get(url)
            found = b"web3-signals" in body.lower() or b"web3 signals" in body.lower()
            listings[name] = {"http": status, "mentions_us": found}
        except Exception as e:
            listings[name] = {"error": str(e)[:80]}
    out["layers"]["listings"] = listings

    return out


def render_markdown(d: dict) -> str:
    lines = [f"# GEO Scorecard — {d['generated_at'][:10]}", ""]
    t = d["layers"].get("traffic", {})
    lines += ["## Traffic (7d)",
              f"- total requests: {t.get('total_requests_7d')}",
              f"- unique clients: {t.get('unique_clients_7d')}",
              f"- AI-crawler hits (top UAs): {t.get('ai_crawler_hits_in_top_uas')}",
              f"- by client type: {t.get('by_client_type')}", ""]
    p = d["layers"].get("payments", {})
    lines += ["## Payments",
              f"- external paid: {p.get('external_paid_calls')} | internal: {p.get('internal_paid_calls')}",
              f"- 402 challenges: {p.get('challenges_402')} | conversion: {p.get('conversion_rate_pct')}%", ""]
    b = d["layers"].get("bazaar", {})
    res = b.get("indexed_resources", [])
    lines += ["## Coinbase Bazaar", f"- indexed resources: {len(res)}"]
    for r in res:
        lines.append(f"  - {r['resource']} (updated {r.get('lastUpdated')}, quality {r.get('quality')})")
    lines.append("")
    lines.append("## Listings")
    for name, st in d["layers"].get("listings", {}).items():
        lines.append(f"- {name}: {st}")
    return "\n".join(lines)


if __name__ == "__main__":
    data = collect()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        print(render_markdown(data))
