from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from shared.base_agent import BaseAgent
from shared.profile_loader import load_profile, get_assets, get_threshold, is_source_enabled
from shared.storage import Storage


class WhaleAgent(BaseAgent):
    """
    Multi-layer whale intelligence engine. Everything from YAML.

    Layer 1: Whale Alert REST API (Primary) — all 20 assets, paginated
    Layer 2: On-chain verification — Etherscan V2 (ETH/ERC-20), Blockchain.com (BTC)
    Layer 3: Exchange flow — balance changes in known exchange wallets
    Layer 4: Known whale wallet tracking — Jump, Wintermute, Galaxy, etc.

    Future: Whale Alert WebSocket socials feed (Layer 1b, disabled)
    Legacy: Twitter whale accounts (Apify), Arkham (disabled by default)
    """

    def __init__(self, profile_path: str | None = None, db_path: str = "signals.db") -> None:
        default = Path(__file__).resolve().parent / "profiles" / "default.yaml"
        self.profile = load_profile(Path(profile_path) if profile_path else default)
        self.assets = get_assets(self.profile)
        self.timeout = int(self.profile.get("http_timeout_sec", 20))
        self.store = Storage(db_path)

        self.etherscan_key = os.getenv("ETHERSCAN_API_KEY", "").strip()
        self.apify_key = os.getenv("APIFY_API_KEY", "").strip()
        self.whale_alert_key = os.getenv("WHALE_ALERT_API_KEY", "").strip()

        super().__init__(
            agent_name="whale_agent",
            profile_name=self.profile.get("name", "whale_default"),
        )

    def empty_data(self) -> Dict[str, Any]:
        lookback = int(self.profile.get("lookback_hours", 24))
        return {
            "whale_moves": [],
            "by_asset": {sym: [] for sym in self.assets},
            "exchange_flow": {},
            "whale_wallets": {},
            "sources_used": [],
            "summary": {
                "total_moves": 0,
                "credible_moves": 0,
                "assets_with_activity": [],
                "net_exchange_direction": "unknown",
                "whale_wallet_signals": [],
                "lookback_hours": lookback,
            },
        }

    def collect(self) -> Tuple[Dict[str, Any], List[str]]:
        data = self.empty_data()
        errors: List[str] = []
        all_moves: List[Dict[str, Any]] = []

        # ============================================================
        # LAYER 1: Whale Alert REST API (Primary)
        # ============================================================
        if is_source_enabled(self.profile, "whale_alert"):
            if self.whale_alert_key:
                try:
                    moves = self._layer_whale_alert_api()
                    all_moves.extend(moves)
                    data["sources_used"].append("whale_alert_api")
                except Exception as exc:
                    errors.append(f"whale_alert_api: {exc}")
            else:
                errors.append("whale_alert_api: WHALE_ALERT_API_KEY not set")

        # ============================================================
        # LAYER 1b: Whale Alert Socials (Future — disabled)
        # ============================================================
        if is_source_enabled(self.profile, "whale_alert_socials"):
            # Placeholder for WebSocket socials feed (v2)
            # Would poll a buffer of parsed social alerts
            # For now this section is a no-op; enabled: false in profile
            try:
                moves = self._layer_whale_alert_socials()
                all_moves.extend(moves)
                data["sources_used"].append("whale_alert_socials")
            except Exception as exc:
                errors.append(f"whale_alert_socials: {exc}")

        # ============================================================
        # LAYER 2: Twitter whale accounts (Supplementary)
        # ============================================================
        if is_source_enabled(self.profile, "twitter_whales"):
            if self.apify_key:
                try:
                    moves = self._layer_twitter_whales()
                    all_moves.extend(moves)
                    data["sources_used"].append("twitter_whales")
                except Exception as exc:
                    errors.append(f"twitter_whales: {exc}")
            else:
                errors.append("twitter_whales: APIFY_API_KEY not set")

        # ============================================================
        # LAYER 3: On-chain verification
        # ============================================================
        if is_source_enabled(self.profile, "etherscan"):
            if self.etherscan_key:
                try:
                    moves = self._layer_etherscan()
                    all_moves.extend(moves)
                    data["sources_used"].append("etherscan")
                except Exception as exc:
                    errors.append(f"etherscan: {exc}")
            else:
                errors.append("etherscan: ETHERSCAN_API_KEY not set")

        if is_source_enabled(self.profile, "blockchain_com"):
            try:
                moves = self._layer_blockchain_com()
                all_moves.extend(moves)
                data["sources_used"].append("blockchain_com")
            except Exception as exc:
                errors.append(f"blockchain_com: {exc}")

        # ============================================================
        # LAYER 4: Exchange flow (balance tracking)
        # ============================================================
        flow_cfg = self.profile.get("exchange_flow", {})
        if flow_cfg.get("enabled", False):
            try:
                data["exchange_flow"] = self._layer_exchange_flow()
                data["sources_used"].append("exchange_flow")
            except Exception as exc:
                errors.append(f"exchange_flow: {exc}")

        # ============================================================
        # LAYER 5: Known whale wallet tracking
        # ============================================================
        wallet_cfg = self.profile.get("whale_wallets", {})
        if wallet_cfg.get("enabled", False):
            try:
                data["whale_wallets"] = self._layer_whale_wallets()
                data["sources_used"].append("whale_wallets")
            except Exception as exc:
                errors.append(f"whale_wallets: {exc}")

        # ============================================================
        # Legacy sources (disabled by default)
        # ============================================================
        if is_source_enabled(self.profile, "arkham"):
            arkham_key = os.getenv("ARKHAM_API_KEY", "").strip()
            if arkham_key:
                try:
                    all_moves.extend(self._legacy_arkham(arkham_key))
                except Exception as exc:
                    errors.append(f"arkham: {exc}")

        # ============================================================
        # Filter, group, summarise
        # ============================================================
        credible = [m for m in all_moves if self._is_credible(m)]

        by_asset: Dict[str, List[Dict[str, Any]]] = {sym: [] for sym in self.assets}
        for move in credible:
            sym = move.get("asset", "").upper()
            if sym in by_asset:
                by_asset[sym].append(move)

        active = [sym for sym, moves in by_asset.items() if moves]

        data["whale_moves"] = credible
        data["by_asset"] = by_asset
        data["summary"] = self._build_summary(
            all_moves, credible, active, data["exchange_flow"], data["whale_wallets"]
        )

        return data, errors

    # ================================================================ #
    # LAYER 1: Whale Alert REST API (Primary)
    # ================================================================ #

    def _layer_whale_alert_api(self) -> List[Dict[str, Any]]:
        """
        Query the Whale Alert REST API with pagination.
        Classifies each transaction based on owner_type of from/to addresses.
        Returns whale moves for all 20 tracked assets.
        """
        cfg = self.profile.get("whale_alert", {})
        base_url = cfg.get("base_url", "https://api.whale-alert.io/v1")
        min_value = int(cfg.get("min_value_usd", 100_000))
        max_per_page = int(cfg.get("max_results_per_page", 100))
        max_pages = int(cfg.get("max_pages", 10))
        action_rules = self.profile.get("action_rules", {})

        # Lookback: use whale_alert-specific or global, default 1h for REST polling
        lookback_sec = int(cfg.get("lookback_sec", 3600))
        if lookback_sec <= 0:
            lookback_sec = int(self.profile.get("lookback_hours", 1)) * 3600
        start_ts = int((datetime.now(timezone.utc) - timedelta(seconds=lookback_sec)).timestamp())

        # Build asset lookup (case-insensitive)
        asset_set_lower = {sym.lower() for sym in self.assets}
        asset_map = {sym.lower(): sym for sym in self.assets}

        moves: List[Dict[str, Any]] = []
        seen_hashes: set = set()
        cursor: Optional[str] = None

        # Rate limit settings from YAML
        rate_cfg = cfg.get("rate_limit", {})
        max_retries = int(rate_cfg.get("max_retries", 3))
        base_delay = float(rate_cfg.get("base_delay_sec", 2.0))
        page_delay = float(rate_cfg.get("page_delay_sec", 1.0))

        for page in range(max_pages):
            params: Dict[str, Any] = {
                "api_key": self.whale_alert_key,
                "min_value": min_value,
                "start": start_ts,
                "limit": max_per_page,
            }
            if cursor:
                params["cursor"] = cursor

            # Retry with exponential backoff on rate limit (429)
            raw = None
            for attempt in range(max_retries):
                try:
                    raw = self._get_json(f"{base_url}/transactions", params)
                    break
                except Exception as exc:
                    if "429" in str(exc) and attempt < max_retries - 1:
                        wait = base_delay * (2 ** attempt)
                        time.sleep(wait)
                    else:
                        raise

            if raw is None:
                break

            transactions = raw.get("transactions", [])
            if not transactions:
                break

            for tx in transactions:
                tx_hash = str(tx.get("hash", ""))
                if tx_hash and tx_hash in seen_hashes:
                    continue
                if tx_hash:
                    seen_hashes.add(tx_hash)

                # Match symbol to our asset list (case-insensitive)
                symbol_raw = str(tx.get("symbol", "")).lower()
                if symbol_raw not in asset_set_lower:
                    continue
                asset = asset_map[symbol_raw]

                # Classify based on owner_type
                from_info = tx.get("from", {}) or {}
                to_info = tx.get("to", {}) or {}
                from_owner_type = str(from_info.get("owner_type", "unknown")).lower()
                to_owner_type = str(to_info.get("owner_type", "unknown")).lower()

                if from_owner_type == "exchange" and to_owner_type != "exchange":
                    # Whale withdrawing from exchange = accumulation (bullish)
                    action = action_rules.get("from_exchange", "accumulate")
                elif from_owner_type != "exchange" and to_owner_type == "exchange":
                    # Whale depositing to exchange = sell pressure (bearish)
                    action = action_rules.get("to_exchange", "sell")
                else:
                    # exchange->exchange or unknown->unknown = transfer (neutral)
                    action = action_rules.get("unknown", "transfer")

                from_label = str(from_info.get("owner", "unknown")) if from_info.get("owner") else "unknown"
                to_label = str(to_info.get("owner", "unknown")) if to_info.get("owner") else "unknown"
                amount_usd = float(tx.get("amount_usd", 0))

                moves.append({
                    "source": "whale_alert_api",
                    "layer": 1,
                    "asset": asset,
                    "amount_usd": amount_usd,
                    "action": action,
                    "from_label": from_label,
                    "to_label": to_label,
                    "tx_hash": tx_hash,
                    "timestamp": str(tx.get("timestamp", "")),
                    "wallet_size_usd": amount_usd,
                    "label": from_label if from_label != "unknown" else to_label,
                    "smart_money_score": None,
                    "blockchain": str(tx.get("blockchain", "")),
                    "amount_native": float(tx.get("amount", 0)),
                    "from_owner_type": from_owner_type,
                    "to_owner_type": to_owner_type,
                })

            # Pagination: check for cursor
            cursor = raw.get("cursor")
            if not cursor:
                break

            # Rate-limit friendly: sleep between pages
            if page < max_pages - 1:
                time.sleep(page_delay)

        return moves

    # ================================================================ #
    # LAYER 1b: Whale Alert Socials (Future — placeholder)
    # ================================================================ #

    def _layer_whale_alert_socials(self) -> List[Dict[str, Any]]:
        """
        Future: Whale Alert WebSocket socials feed.
        Requires persistent connection — not suitable for 15-min polling.
        This is a structural placeholder for v2.

        When implemented, this would:
        - Maintain a buffer of recent social alerts from the WS feed
        - Parse `text` for asset mentions and direction keywords
        - Return whale moves with source="whale_alert_socials"
        """
        # No-op: this layer is disabled in the default profile
        # Structure preserved for future WebSocket integration
        return []

    # ================================================================ #
    # LAYER 2: Twitter whale accounts (Apify — Supplementary)
    # ================================================================ #

    def _layer_twitter_whales(self) -> List[Dict[str, Any]]:
        cfg = self.profile["twitter_whales"]
        actor_id = cfg.get("actor_id", "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest")
        run_timeout = int(cfg.get("run_timeout_sec", 60))
        tweets_per_search = int(cfg.get("tweets_per_search", 30))
        min_usd = float(cfg.get("min_usd_mentioned", 1_000_000))
        queries = cfg.get("search_queries", [])
        direction_kws = cfg.get("direction_keywords", {})
        action_rules = self.profile.get("action_rules", {})

        url = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items?token={self.apify_key}&timeout={run_timeout}"

        moves: List[Dict[str, Any]] = []
        seen_ids: set = set()

        for query in queries:
            try:
                payload = json.dumps({
                    "searchTerms": [query],
                    "maxItems": tweets_per_search,
                    "searchMode": "live",
                }).encode()

                req = Request(url, data=payload, headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                })

                with urlopen(req, timeout=run_timeout + 30) as resp:
                    items = json.loads(resp.read().decode("utf-8"))

                for tweet in items:
                    tweet_id = tweet.get("id", "")
                    if not tweet_id or tweet_id in seen_ids:
                        continue
                    seen_ids.add(tweet_id)

                    if "demo" in tweet and len(tweet) == 1:
                        continue

                    text = str(tweet.get("text", ""))
                    text_lower = text.lower()

                    usd_amount = self._extract_usd_amount(text)
                    if usd_amount < min_usd:
                        continue

                    matched_asset = self._match_asset_in_text(text_lower)
                    if not matched_asset:
                        continue

                    text_clean = text_lower.replace("#", "")
                    action = self._classify_action(text_clean, direction_kws, action_rules)
                    from_label, to_label = self._extract_labels(text)

                    author = tweet.get("author", {})
                    source_account = author.get("userName", "unknown") if isinstance(author, dict) else "unknown"

                    moves.append({
                        "source": f"twitter:{source_account}",
                        "layer": 2,
                        "asset": matched_asset,
                        "amount_usd": usd_amount,
                        "action": action,
                        "from_label": from_label,
                        "to_label": to_label,
                        "tx_hash": "",
                        "timestamp": str(tweet.get("createdAt", "")),
                        "wallet_size_usd": usd_amount,
                        "label": from_label if from_label != "unknown" else to_label,
                        "smart_money_score": None,
                        "tweet_text": text[:200],
                    })

            except Exception:
                continue

        return moves

    # ================================================================ #
    # LAYER 3a: Etherscan V2 — ETH + ERC-20 large transfers
    # ================================================================ #

    def _layer_etherscan(self) -> List[Dict[str, Any]]:
        cfg = self.profile["etherscan"]
        base = cfg.get("base_url", "https://api.etherscan.io/v2/api")
        chain_id = int(cfg.get("chain_id", 1))
        min_eth = float(cfg.get("min_eth_value", 100))
        max_txs = int(cfg.get("max_txs_per_wallet", 20))
        exchange_wallets = cfg.get("exchange_wallets", {})
        action_rules = self.profile.get("action_rules", {})

        moves: List[Dict[str, Any]] = []
        seen_hashes: set = set()

        for exchange_name, addresses in exchange_wallets.items():
            for addr in addresses:
                try:
                    # Fetch recent ETH transfers
                    params = {
                        "chainid": chain_id,
                        "module": "account",
                        "action": "txlist",
                        "address": addr,
                        "page": 1,
                        "offset": max_txs,
                        "sort": "desc",
                        "apikey": self.etherscan_key,
                    }
                    data = self._get_json(f"{base}", params)

                    for tx in data.get("result", []):
                        if not isinstance(tx, dict):
                            continue
                        tx_hash = tx.get("hash", "")
                        if tx_hash in seen_hashes:
                            continue
                        seen_hashes.add(tx_hash)

                        value_eth = int(tx.get("value", 0)) / 1e18
                        if value_eth < min_eth:
                            continue

                        from_addr = tx.get("from", "").lower()
                        to_addr = tx.get("to", "").lower()
                        is_inflow = to_addr == addr.lower()

                        action = action_rules.get("to_exchange", "sell") if is_inflow else action_rules.get("from_exchange", "accumulate")

                        moves.append({
                            "source": "etherscan",
                            "layer": 3,
                            "asset": "ETH",
                            "amount_usd": 0.0,  # will be enriched by market agent
                            "amount_native": round(value_eth, 4),
                            "action": action,
                            "from_label": exchange_name if not is_inflow else "unknown",
                            "to_label": exchange_name if is_inflow else "unknown",
                            "tx_hash": tx_hash,
                            "timestamp": tx.get("timeStamp", ""),
                            "wallet_size_usd": 0.0,
                            "label": exchange_name,
                            "smart_money_score": None,
                        })

                except Exception:
                    continue

                # Also fetch ERC-20 token transfers
                try:
                    token_params = {
                        "chainid": chain_id,
                        "module": "account",
                        "action": "tokentx",
                        "address": addr,
                        "page": 1,
                        "offset": max_txs,
                        "sort": "desc",
                        "apikey": self.etherscan_key,
                    }
                    token_data = self._get_json(f"{base}", token_params)

                    for tx in token_data.get("result", []):
                        if not isinstance(tx, dict):
                            continue
                        tx_hash = tx.get("hash", "")
                        if tx_hash in seen_hashes:
                            continue
                        seen_hashes.add(tx_hash)

                        decimals = int(tx.get("tokenDecimal", 18))
                        value = int(tx.get("value", 0)) / 10**decimals
                        token_sym = str(tx.get("tokenSymbol", "")).upper()

                        # Only track our assets
                        if token_sym not in self.assets:
                            continue

                        from_addr = tx.get("from", "").lower()
                        to_addr = tx.get("to", "").lower()
                        is_inflow = to_addr == addr.lower()
                        action = action_rules.get("to_exchange", "sell") if is_inflow else action_rules.get("from_exchange", "accumulate")

                        moves.append({
                            "source": "etherscan",
                            "layer": 3,
                            "asset": token_sym,
                            "amount_usd": 0.0,
                            "amount_native": round(value, 4),
                            "action": action,
                            "from_label": exchange_name if not is_inflow else "unknown",
                            "to_label": exchange_name if is_inflow else "unknown",
                            "tx_hash": tx_hash,
                            "timestamp": tx.get("timeStamp", ""),
                            "wallet_size_usd": 0.0,
                            "label": exchange_name,
                            "smart_money_score": None,
                        })

                except Exception:
                    continue

        return moves

    # ================================================================ #
    # LAYER 3b: Blockchain.com — BTC large transfers
    # ================================================================ #

    def _layer_blockchain_com(self) -> List[Dict[str, Any]]:
        cfg = self.profile["blockchain_com"]
        base = cfg.get("base_url", "https://blockchain.info")
        min_btc = float(cfg.get("min_btc_value", 10))
        max_txs = int(cfg.get("max_txs_per_wallet", 10))
        exchange_wallets = cfg.get("exchange_wallets", {})
        action_rules = self.profile.get("action_rules", {})

        moves: List[Dict[str, Any]] = []
        seen_hashes: set = set()

        for exchange_name, addresses in exchange_wallets.items():
            for addr in addresses:
                try:
                    url = f"{base}/rawaddr/{addr}?limit={max_txs}"
                    data = self._get_json(url)

                    for tx in data.get("txs", []):
                        tx_hash = tx.get("hash", "")
                        if tx_hash in seen_hashes:
                            continue
                        seen_hashes.add(tx_hash)

                        result_sat = tx.get("result", 0)
                        result_btc = abs(result_sat) / 1e8

                        if result_btc < min_btc:
                            continue

                        # result > 0 = wallet received BTC (inflow to exchange)
                        # result < 0 = wallet sent BTC (outflow from exchange)
                        is_inflow = result_sat > 0
                        action = action_rules.get("to_exchange", "sell") if is_inflow else action_rules.get("from_exchange", "accumulate")

                        moves.append({
                            "source": "blockchain_com",
                            "layer": 3,
                            "asset": "BTC",
                            "amount_usd": 0.0,
                            "amount_native": round(result_btc, 8),
                            "action": action,
                            "from_label": "unknown" if is_inflow else exchange_name,
                            "to_label": exchange_name if is_inflow else "unknown",
                            "tx_hash": tx_hash,
                            "timestamp": str(tx.get("time", "")),
                            "wallet_size_usd": 0.0,
                            "label": exchange_name,
                            "smart_money_score": None,
                        })

                except Exception:
                    continue

        return moves

    # ================================================================ #
    # LAYER 4: Exchange flow — balance snapshots over time
    # ================================================================ #

    def _layer_exchange_flow(self) -> Dict[str, Any]:
        flow_cfg = self.profile.get("exchange_flow", {})
        track_exchanges = flow_cfg.get("track_exchanges", [])
        eth_threshold = float(flow_cfg.get("eth_significant_change", 1000))
        btc_threshold = float(flow_cfg.get("btc_significant_change", 100))

        flows: Dict[str, Any] = {}

        for exchange in track_exchanges:
            exchange_flow: Dict[str, Any] = {"eth_balance": None, "btc_balance": None, "eth_change": None, "btc_change": None, "direction": "unknown"}

            # ETH balance
            eth_cfg = self.profile.get("etherscan", {})
            eth_addrs = eth_cfg.get("exchange_wallets", {}).get(exchange, [])
            if eth_addrs and self.etherscan_key:
                total_eth = 0.0
                for addr in eth_addrs:
                    try:
                        params = {
                            "chainid": int(eth_cfg.get("chain_id", 1)),
                            "module": "account",
                            "action": "balance",
                            "address": addr,
                            "tag": "latest",
                            "apikey": self.etherscan_key,
                        }
                        data = self._get_json(eth_cfg.get("base_url", "https://api.etherscan.io/v2/api"), params)
                        if data.get("status") == "1":
                            total_eth += int(data["result"]) / 1e18
                    except Exception:
                        continue
                exchange_flow["eth_balance"] = round(total_eth, 2)

                # Compare with stored previous balance
                prev = self._load_flow_snapshot(exchange, "eth")
                if prev is not None:
                    change = total_eth - prev
                    exchange_flow["eth_change"] = round(change, 2)
                self._store_flow_snapshot(exchange, "eth", total_eth)

            # BTC balance
            btc_cfg = self.profile.get("blockchain_com", {})
            btc_addrs = btc_cfg.get("exchange_wallets", {}).get(exchange, [])
            if btc_addrs:
                total_btc = 0.0
                for addr in btc_addrs:
                    try:
                        url = f"{btc_cfg.get('base_url', 'https://blockchain.info')}/balance?active={addr}"
                        data = self._get_json(url)
                        for _, info in data.items():
                            total_btc += info.get("final_balance", 0) / 1e8
                    except Exception:
                        continue
                exchange_flow["btc_balance"] = round(total_btc, 4)

                prev = self._load_flow_snapshot(exchange, "btc")
                if prev is not None:
                    change = total_btc - prev
                    exchange_flow["btc_change"] = round(change, 4)
                self._store_flow_snapshot(exchange, "btc", total_btc)

            # Determine direction
            eth_chg = exchange_flow.get("eth_change") or 0
            btc_chg = exchange_flow.get("btc_change") or 0
            if eth_chg > eth_threshold or btc_chg > btc_threshold:
                exchange_flow["direction"] = "inflow"  # money coming IN = sell pressure
            elif eth_chg < -eth_threshold or btc_chg < -btc_threshold:
                exchange_flow["direction"] = "outflow"  # money going OUT = accumulation
            else:
                exchange_flow["direction"] = "neutral"

            flows[exchange] = exchange_flow

        return flows

    # ================================================================ #
    # LAYER 5: Known whale wallet balance tracking
    # ================================================================ #

    def _layer_whale_wallets(self) -> Dict[str, Any]:
        wallet_cfg = self.profile.get("whale_wallets", {})
        min_eth_change = float(wallet_cfg.get("min_eth_change", 50))
        min_btc_change = float(wallet_cfg.get("min_btc_change", 5))

        results: Dict[str, Any] = {}

        # ETH whale wallets
        eth_wallets = wallet_cfg.get("eth_wallets", {})
        eth_cfg = self.profile.get("etherscan", {})
        for name, info in eth_wallets.items():
            addr = info.get("address", "")
            if not addr or not self.etherscan_key:
                continue
            try:
                params = {
                    "chainid": int(eth_cfg.get("chain_id", 1)),
                    "module": "account",
                    "action": "balance",
                    "address": addr,
                    "tag": "latest",
                    "apikey": self.etherscan_key,
                }
                data = self._get_json(eth_cfg.get("base_url", "https://api.etherscan.io/v2/api"), params)
                if data.get("status") != "1":
                    continue

                balance_eth = int(data["result"]) / 1e18
                prev = self._load_flow_snapshot(f"whale_{name}", "eth")
                change = (balance_eth - prev) if prev is not None else 0.0
                self._store_flow_snapshot(f"whale_{name}", "eth", balance_eth)

                signal = "neutral"
                if abs(change) >= min_eth_change:
                    signal = "accumulating" if change > 0 else "reducing"

                results[name] = {
                    "chain": "ETH",
                    "address": addr[:12] + "...",
                    "balance_eth": round(balance_eth, 2),
                    "change_eth": round(change, 2),
                    "signal": signal,
                }
            except Exception:
                continue

        # BTC whale wallets
        btc_wallets = wallet_cfg.get("btc_wallets", {})
        btc_cfg = self.profile.get("blockchain_com", {})
        for name, info in btc_wallets.items():
            addr = info.get("address", "")
            if not addr:
                continue
            try:
                url = f"{btc_cfg.get('base_url', 'https://blockchain.info')}/balance?active={addr}"
                data = self._get_json(url)
                balance_btc = 0.0
                for _, bal_info in data.items():
                    balance_btc += bal_info.get("final_balance", 0) / 1e8

                prev = self._load_flow_snapshot(f"whale_{name}", "btc")
                change = (balance_btc - prev) if prev is not None else 0.0
                self._store_flow_snapshot(f"whale_{name}", "btc", balance_btc)

                signal = "neutral"
                if abs(change) >= min_btc_change:
                    signal = "accumulating" if change > 0 else "reducing"

                results[name] = {
                    "chain": "BTC",
                    "address": addr[:12] + "...",
                    "balance_btc": round(balance_btc, 4),
                    "change_btc": round(change, 4),
                    "signal": signal,
                }
            except Exception:
                continue

        return results

    # ================================================================ #
    # Legacy sources (disabled by default)
    # ================================================================ #

    def _legacy_arkham(self, api_key: str) -> List[Dict[str, Any]]:
        cfg = self.profile["arkham"]
        lookback = int(self.profile.get("lookback_hours", 24))
        headers = {"API-Key": api_key, "Content-Type": "application/json"}
        params = {"limit": int(cfg.get("max_results", 50)), "timerange": f"{lookback}h", "entityType": cfg.get("entity_type", "smart_money")}
        raw = self._get_json_with_headers(f"{cfg['base_url']}/transfers", params, headers)
        moves = []
        for tx in raw.get("transfers", []):
            token = tx.get("tokenSymbol", "").upper()
            if token not in self.assets:
                continue
            entity = tx.get("fromEntity", {})
            moves.append({"source": "arkham", "layer": 0, "asset": token, "amount_usd": float(tx.get("historicalUSD", 0)), "action": "accumulate" if tx.get("toEntity", {}).get("isSmartMoney") else "transfer", "from_label": str(entity.get("name", "unknown")), "to_label": str(tx.get("toEntity", {}).get("name", "unknown")), "tx_hash": str(tx.get("txnHash", "")), "timestamp": str(tx.get("blockTimestamp", "")), "wallet_size_usd": float(entity.get("usdValue", 0)), "label": str(entity.get("name", "unknown")), "smart_money_score": float(entity.get("smartMoneyScore", 0))})
        return moves

    # ================================================================ #
    # Credibility filter
    # ================================================================ #

    def _is_credible(self, move: Dict[str, Any]) -> bool:
        cred = self.profile.get("credibility", {})
        min_size = float(cred.get("min_wallet_size_usd", 1_000_000))

        source = str(move.get("source", ""))

        # Whale Alert API: always credible (verified on-chain transactions)
        if source == "whale_alert_api":
            return True

        # Twitter-sourced: already filtered by min_usd_mentioned
        if source.startswith("twitter:"):
            return True

        # On-chain sourced: from tracked exchange wallets = credible by definition
        if source in ("etherscan", "blockchain_com"):
            return True

        # Whale Alert Socials: credible when implemented (verified feed)
        if source == "whale_alert_socials":
            return True

        # API sources: apply size + label filter
        if move.get("amount_usd", 0) < min_size and move.get("wallet_size_usd", 0) < min_size:
            return False

        return True

    # ================================================================ #
    # Summary builder
    # ================================================================ #

    def _build_summary(
        self,
        all_moves: List[Dict[str, Any]],
        credible: List[Dict[str, Any]],
        active_assets: List[str],
        exchange_flow: Dict[str, Any],
        whale_wallets: Dict[str, Any],
    ) -> Dict[str, Any]:
        # Net exchange direction
        inflow_count = sum(1 for ex in exchange_flow.values() if isinstance(ex, dict) and ex.get("direction") == "inflow")
        outflow_count = sum(1 for ex in exchange_flow.values() if isinstance(ex, dict) and ex.get("direction") == "outflow")
        if outflow_count > inflow_count:
            net_direction = "net_outflow"  # accumulation signal
        elif inflow_count > outflow_count:
            net_direction = "net_inflow"  # selling signal
        else:
            net_direction = "neutral"

        # Whale wallet signals
        whale_signals = []
        for name, info in whale_wallets.items():
            if isinstance(info, dict) and info.get("signal") != "neutral":
                whale_signals.append(f"{name}: {info['signal']}")

        # Whale Alert API stats
        wa_moves = [m for m in credible if m.get("source") == "whale_alert_api"]
        wa_accumulate = sum(1 for m in wa_moves if m.get("action") == "accumulate")
        wa_sell = sum(1 for m in wa_moves if m.get("action") == "sell")

        return {
            "total_moves": len(all_moves),
            "credible_moves": len(credible),
            "assets_with_activity": active_assets,
            "net_exchange_direction": net_direction,
            "whale_wallet_signals": whale_signals,
            "lookback_hours": int(self.profile.get("lookback_hours", 24)),
            "whale_alert_api_stats": {
                "total": len(wa_moves),
                "accumulate": wa_accumulate,
                "sell": wa_sell,
                "transfer": len(wa_moves) - wa_accumulate - wa_sell,
            },
        }

    # ================================================================ #
    # Flow snapshot storage (uses shared Storage — Postgres or SQLite)
    # ================================================================ #

    def _load_flow_snapshot(self, entity: str, chain: str) -> Optional[float]:
        return self.store.load_kv("whale_flow", f"{entity}:{chain}")

    def _store_flow_snapshot(self, entity: str, chain: str, balance: float) -> None:
        self.store.save_kv("whale_flow", f"{entity}:{chain}", balance)

    # ================================================================ #
    # Text parsing helpers
    # ================================================================ #

    def _match_asset_in_text(self, text_lower: str) -> Optional[str]:
        for sym in self.assets:
            if f"#{sym.lower()}" in text_lower or f"${sym.lower()}" in text_lower or f" {sym.lower()} " in text_lower:
                return sym
        return None

    @staticmethod
    def _extract_usd_amount(text: str) -> float:
        patterns = [r'([\d,]+(?:\.\d+)?)\s*USD', r'\$([\d,]+(?:\.\d+)?)']
        best = 0.0
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                try:
                    val = float(match.group(1).replace(",", ""))
                    if val > best:
                        best = val
                except (ValueError, IndexError):
                    continue
        return best

    @staticmethod
    def _classify_action(text_lower: str, direction_kws: Dict[str, List[str]], action_rules: Dict[str, str]) -> str:
        for kw in direction_kws.get("to_exchange", []):
            if kw.lower() in text_lower:
                return action_rules.get("to_exchange", "sell")
        for kw in direction_kws.get("from_exchange", []):
            if kw.lower() in text_lower:
                return action_rules.get("from_exchange", "accumulate")
        for kw in direction_kws.get("accumulate", []):
            if kw.lower() in text_lower:
                return "accumulate"
        for kw in direction_kws.get("sell", []):
            if kw.lower() in text_lower:
                return "sell"
        return action_rules.get("unknown", "transfer")

    @staticmethod
    def _extract_labels(text: str) -> Tuple[str, str]:
        from_label = "unknown"
        to_label = "unknown"
        from_match = re.search(r'(?:from|transferred from)\s+#?(\w[\w\s]*?)(?:\s+to\b|\s*$)', text, re.IGNORECASE)
        if from_match:
            from_label = from_match.group(1).strip()
        to_match = re.search(r'(?:to|transferred to)\s+#?(\w[\w\s]*?)(?:\s*$|\s*\n|\s*http)', text, re.IGNORECASE)
        if to_match:
            to_label = to_match.group(1).strip()
        return from_label, to_label

    # ================================================================ #
    # HTTP helpers
    # ================================================================ #

    def _get_json(self, url: str, params: Dict[str, Any] | None = None) -> Any:
        full_url = f"{url}?{urlencode(params)}" if params else url
        req = Request(full_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get_json_with_headers(self, url: str, params: Dict[str, Any], headers: Dict[str, str]) -> Any:
        full_url = f"{url}?{urlencode(params)}" if params else url
        req = Request(full_url, headers={**{"User-Agent": "Mozilla/5.0"}, **headers})
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
