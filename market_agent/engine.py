from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from shared.base_agent import BaseAgent
from shared.profile_loader import load_profile, get_assets, get_threshold, is_source_enabled

# Optional yfinance import — macro data skipped if unavailable
try:
    import yfinance as yf  # type: ignore
    _HAS_YFINANCE = True
except ImportError:
    _HAS_YFINANCE = False


class MarketAgent(BaseAgent):
    """
    Tracks broad market health + per-asset price/volume for our tracked assets.
    Everything is driven by profiles/default.yaml — no hardcoded values.

    Data sections (each toggleable via YAML):
      1. per_asset     — price, 24h change, volume, volume spike for our 20 assets
      2. breadth       — top gainers/losers/trending from top 100
      3. categories    — sector performance (DeFi, L1, L2, etc.)
      4. global_market — total market cap, BTC dominance, 24h change
      5. dex           — top DEX pairs by volume (DexScreener)
      6. sentiment     — Fear & Greed index
      7. order_book    — Binance order book depth + imbalance
      8. stablecoin    — USDT/USDC market cap flow proxy
      9. macro         — S&P 500, VIX, DXY correlations (via yfinance)
    """

    def __init__(self, profile_path: str | None = None) -> None:
        default = Path(__file__).resolve().parent / "profiles" / "default.yaml"
        self.profile = load_profile(Path(profile_path) if profile_path else default)
        self.assets = get_assets(self.profile)
        self.timeout = int(self.profile.get("http_timeout_sec", 20))

        self.cg_id_map: Dict[str, str] = self.profile.get("coingecko_id_map", {})
        self.bn_symbol_map: Dict[str, str] = self.profile.get("binance_symbol_map", {})
        self.cg_cfg = self.profile.get("coingecko", {})
        self.bn_cfg = self.profile.get("binance", {})
        self.dex_cfg = self.profile.get("dexscreener", {})
        self.fg_cfg = self.profile.get("fear_greed", {})
        self.stablecoin_cfg = self.profile.get("stablecoin", {})
        self.macro_cfg = self.profile.get("macro", {})
        self.scoring_cfg = self.profile.get("scoring", {})

        super().__init__(
            agent_name="market_agent",
            profile_name=self.profile.get("name", "market_default"),
        )

    def empty_data(self) -> Dict[str, Any]:
        return {
            "per_asset": {},
            "breadth": {"top_gainers": [], "top_losers": [], "trending_tokens": []},
            "categories": {"top_gainers": [], "top_losers": []},
            "global_market": {
                "total_market_cap_usd": None,
                "total_market_cap_change_24h": None,
                "btc_dominance": None,
                "eth_dominance": None,
                "active_cryptocurrencies": None,
            },
            "dex": {"top_pairs": []},
            "sentiment": {"fear_greed_index": None, "classification": None},
            "stablecoin": {
                "usdt_market_cap": None,
                "usdc_market_cap": None,
                "total_mcap": None,
                "change_24h_pct": None,
                "flow_status": "unknown",
            },
            "macro": {
                "sp500_price": None,
                "sp500_change_1d_pct": None,
                "vix_level": None,
                "vix_status": "unknown",
                "dxy_price": None,
                "dxy_change_1d_pct": None,
                "risk_status": "unknown",
            },
            "summary": {
                "volume_spike_assets": [],
                "elevated_volume_assets": [],
                "top_gainer_asset": None,
                "top_loser_asset": None,
                "market_direction": None,
            },
        }

    def collect(self) -> Tuple[Dict[str, Any], List[str]]:
        data = self.empty_data()
        errors: List[str] = []

        # --- 1. Per-asset data from CoinGecko ---
        if is_source_enabled(self.profile, "coingecko"):
            try:
                data["per_asset"] = self._fetch_per_asset()
            except Exception as exc:
                errors.append(f"per_asset: {exc}")

        # --- 2. Volume spike from Binance klines ---
        if is_source_enabled(self.profile, "binance"):
            try:
                self._enrich_volume_spikes(data["per_asset"])
            except Exception as exc:
                errors.append(f"volume_spikes: {exc}")

            # --- 2b. ATR(14) from Binance klines ---
            try:
                self._enrich_atr(data["per_asset"])
            except Exception as exc:
                errors.append(f"atr: {exc}")

        # --- 3. Order book depth from Binance ---
        ob_cfg = self.bn_cfg.get("order_book", {})
        if ob_cfg.get("enabled", True) and is_source_enabled(self.profile, "binance"):
            try:
                self._enrich_order_book(data["per_asset"])
            except Exception as exc:
                errors.append(f"order_book: {exc}")

        # --- 4. Broad market breadth ---
        market_sample: List[Dict[str, Any]] = []
        breadth_cfg = self.cg_cfg.get("breadth", {})
        if breadth_cfg.get("enabled", True) and is_source_enabled(self.profile, "coingecko"):
            try:
                market_sample = self._fetch_market_sample(breadth_cfg)
                data["breadth"]["top_gainers"], data["breadth"]["top_losers"] = \
                    self._build_gainers_losers(market_sample, breadth_cfg)
            except Exception as exc:
                errors.append(f"breadth: {exc}")

            trending_cfg = self.cg_cfg.get("trending", {})
            if trending_cfg.get("enabled", True):
                try:
                    data["breadth"]["trending_tokens"] = self._fetch_trending(trending_cfg)
                except Exception as exc:
                    errors.append(f"trending: {exc}")

        # --- 5. Categories ---
        cat_cfg = self.cg_cfg.get("categories", {})
        if cat_cfg.get("enabled", True) and is_source_enabled(self.profile, "coingecko"):
            try:
                data["categories"] = self._fetch_categories(cat_cfg)
            except Exception as exc:
                errors.append(f"categories: {exc}")

        # --- 6. Global market data ---
        global_cfg = self.cg_cfg.get("global", {})
        if global_cfg.get("enabled", True) and is_source_enabled(self.profile, "coingecko"):
            try:
                data["global_market"] = self._fetch_global()
            except Exception as exc:
                errors.append(f"global_market: {exc}")

        # --- 7. DexScreener ---
        if is_source_enabled(self.profile, "dexscreener"):
            try:
                data["dex"]["top_pairs"] = self._fetch_dex_pairs()
            except Exception as exc:
                errors.append(f"dex: {exc}")

        # --- 8. Fear & Greed ---
        if is_source_enabled(self.profile, "fear_greed"):
            try:
                data["sentiment"] = self._fetch_sentiment()
            except Exception as exc:
                errors.append(f"sentiment: {exc}")

        # --- 9. Stablecoin flow proxy ---
        if self.stablecoin_cfg.get("enabled", False):
            try:
                data["stablecoin"] = self._fetch_stablecoin()
            except Exception as exc:
                errors.append(f"stablecoin: {exc}")

        # --- 10. Macro correlations ---
        if self.macro_cfg.get("enabled", False):
            try:
                data["macro"] = self._fetch_macro()
            except Exception as exc:
                errors.append(f"macro: {exc}")

        # --- Build summary ---
        data["summary"] = self._build_summary(data)

        # --- Compute per-asset market_score ---
        self._compute_market_scores(data)

        return data, errors

    # ------------------------------------------------------------------ #
    # 1. Per-asset price/volume from CoinGecko
    # ------------------------------------------------------------------ #

    def _fetch_per_asset(self) -> Dict[str, Dict[str, Any]]:
        base_url = self.cg_cfg.get("base_url", "https://api.coingecko.com/api/v3")
        vs = self.cg_cfg.get("vs_currency", "usd")

        # Build comma-separated CoinGecko IDs for our assets
        cg_ids = []
        sym_by_id: Dict[str, str] = {}
        for sym in self.assets:
            cg_id = self.cg_id_map.get(sym)
            if cg_id:
                cg_ids.append(cg_id)
                sym_by_id[cg_id] = sym

        if not cg_ids:
            return {}

        payload = self._get_json(
            f"{base_url}/simple/price",
            params={
                "ids": ",".join(cg_ids),
                "vs_currencies": vs,
                "include_market_cap": str(bool(self.cg_cfg.get("include_market_cap", True))).lower(),
                "include_24hr_vol": str(bool(self.cg_cfg.get("include_24hr_vol", True))).lower(),
                "include_24hr_change": str(bool(self.cg_cfg.get("include_24hr_change", True))).lower(),
            },
        )

        result: Dict[str, Dict[str, Any]] = {}
        for cg_id, sym in sym_by_id.items():
            coin = payload.get(cg_id, {})
            result[sym] = {
                "price": self._to_float(coin.get(vs)),
                "change_24h_pct": self._to_float(coin.get(f"{vs}_24h_change")),
                "volume_24h": self._to_float(coin.get(f"{vs}_24h_vol")),
                "market_cap": self._to_float(coin.get(f"{vs}_market_cap")),
                "volume_7d_avg": None,
                "volume_spike_ratio": None,
                "volume_status": "unknown",
                "order_book_imbalance": None,
                "bid_wall_usd": None,
                "ask_wall_usd": None,
                "atr_14": None,
                "atr_pct": None,
                "market_score": None,
            }

        return result

    # ------------------------------------------------------------------ #
    # 2. Volume spike enrichment from Binance klines
    # ------------------------------------------------------------------ #

    def _enrich_volume_spikes(self, per_asset: Dict[str, Dict[str, Any]]) -> None:
        base_url = self.bn_cfg.get("base_url", "https://api.binance.com/api/v3")
        ep = self.bn_cfg.get("klines_endpoint", "/klines")
        interval = self.bn_cfg.get("interval", "1d")
        lookback = int(self.bn_cfg.get("lookback_days", 8))

        spike_cfg = self.bn_cfg.get("volume_spike", {})
        spike_thresh = float(spike_cfg.get("spike_threshold", 2.0))
        high_thresh = float(spike_cfg.get("high_threshold", 1.5))

        for sym in self.assets:
            if sym not in per_asset:
                continue

            bn_sym = self.bn_symbol_map.get(sym)
            if not bn_sym:
                continue

            try:
                url = f"{base_url}{ep}?symbol={bn_sym}&interval={interval}&limit={lookback}"
                raw = self._get_json(url)

                # Binance kline: [open_time, open, high, low, close, volume, ...]
                volumes = [float(candle[5]) for candle in raw]  # index 5 = quote volume

                if len(volumes) < 2:
                    continue

                today_vol = volumes[-1]
                avg_7d = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else today_vol

                ratio = today_vol / avg_7d if avg_7d > 0 else 0.0

                per_asset[sym]["volume_7d_avg"] = round(avg_7d, 2)
                per_asset[sym]["volume_spike_ratio"] = round(ratio, 2)

                if ratio >= spike_thresh:
                    per_asset[sym]["volume_status"] = "spike"
                elif ratio >= high_thresh:
                    per_asset[sym]["volume_status"] = "elevated"
                else:
                    per_asset[sym]["volume_status"] = "normal"

            except Exception:
                continue  # per-asset volume failure is non-fatal

    # ------------------------------------------------------------------ #
    # 2b. ATR(14) enrichment from Binance klines
    # ------------------------------------------------------------------ #

    def _enrich_atr(self, per_asset: Dict[str, Dict[str, Any]]) -> None:
        """Calculate ATR(14) for each asset using Binance daily klines."""
        base_url = self.bn_cfg.get("base_url", "https://api.binance.com/api/v3")
        ep = self.bn_cfg.get("klines_endpoint", "/klines")
        atr_period = 14
        # Need atr_period + 1 candles minimum (for prev close), fetch 20 to be safe
        lookback = max(20, atr_period + 2)

        for sym in self.assets:
            if sym not in per_asset:
                continue

            bn_sym = self.bn_symbol_map.get(sym)
            if not bn_sym:
                continue

            try:
                url = f"{base_url}{ep}?symbol={bn_sym}&interval=1d&limit={lookback}"
                raw = self._get_json(url)

                if len(raw) < atr_period + 1:
                    continue

                # Binance kline: [open_time, open, high, low, close, ...]
                highs = [float(c[2]) for c in raw]
                lows = [float(c[3]) for c in raw]
                closes = [float(c[4]) for c in raw]

                # Calculate True Range series (need prev close, so start at index 1)
                tr_list: List[float] = []
                for i in range(1, len(raw)):
                    h = highs[i]
                    l = lows[i]
                    prev_c = closes[i - 1]
                    tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                    tr_list.append(tr)

                if len(tr_list) < atr_period:
                    continue

                # Wilder smoothing: first ATR = simple average, then EMA-style
                atr = sum(tr_list[:atr_period]) / atr_period
                for tr_val in tr_list[atr_period:]:
                    atr = (atr * (atr_period - 1) + tr_val) / atr_period

                current_close = closes[-1]
                atr_pct = (atr / current_close * 100) if current_close > 0 else 0.0

                per_asset[sym]["atr_14"] = round(atr, 6)
                per_asset[sym]["atr_pct"] = round(atr_pct, 2)

            except Exception:
                continue  # per-asset ATR failure is non-fatal

    # ------------------------------------------------------------------ #
    # 3. Order book depth from Binance
    # ------------------------------------------------------------------ #

    def _enrich_order_book(self, per_asset: Dict[str, Dict[str, Any]]) -> None:
        """Fetch order book depth for each asset and compute imbalance."""
        base_url = self.bn_cfg.get("base_url", "https://api.binance.com/api/v3")
        ob_cfg = self.bn_cfg.get("order_book", {})
        depth_limit = int(ob_cfg.get("depth_limit", 20))
        rate_sleep = float(ob_cfg.get("rate_limit_sleep", 0.1))

        for sym in self.assets:
            if sym not in per_asset:
                continue

            bn_sym = self.bn_symbol_map.get(sym)
            if not bn_sym:
                continue

            try:
                url = f"{base_url}/depth?symbol={bn_sym}&limit={depth_limit}"
                book = self._get_json(url)

                # bids/asks are [[price, qty], ...]
                bid_wall = sum(
                    float(level[0]) * float(level[1])
                    for level in book.get("bids", [])
                )
                ask_wall = sum(
                    float(level[0]) * float(level[1])
                    for level in book.get("asks", [])
                )

                imbalance = bid_wall / ask_wall if ask_wall > 0 else 0.0

                per_asset[sym]["bid_wall_usd"] = round(bid_wall, 2)
                per_asset[sym]["ask_wall_usd"] = round(ask_wall, 2)
                per_asset[sym]["order_book_imbalance"] = round(imbalance, 4)

            except Exception:
                continue  # per-asset order book failure is non-fatal

            time.sleep(rate_sleep)

    # ------------------------------------------------------------------ #
    # 4. Broad market breadth
    # ------------------------------------------------------------------ #

    def _fetch_market_sample(self, breadth_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        base_url = self.cg_cfg.get("base_url", "https://api.coingecko.com/api/v3")
        vs = self.cg_cfg.get("vs_currency", "usd")
        sample = min(int(breadth_cfg.get("market_sample", 100)), 250)

        return self._get_json(
            f"{base_url}/coins/markets",
            params={
                "vs_currency": vs,
                "order": "market_cap_desc",
                "per_page": sample,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h",
            },
        )

    def _build_gainers_losers(
        self, coins: List[Dict[str, Any]], breadth_cfg: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        gainers_count = int(breadth_cfg.get("top_gainers_count", 10))
        losers_count = int(breadth_cfg.get("top_losers_count", 10))

        by_change = sorted(
            coins,
            key=lambda c: self._to_float(c.get("price_change_percentage_24h")),
            reverse=True,
        )
        gainers = [self._normalize_coin(c) for c in by_change[:gainers_count]]
        losers = [self._normalize_coin(c) for c in by_change[-losers_count:]]
        return gainers, losers

    def _fetch_trending(self, trending_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        base_url = self.cg_cfg.get("base_url", "https://api.coingecko.com/api/v3")
        count = int(trending_cfg.get("count", 7))

        payload = self._get_json(f"{base_url}/search/trending")
        trending = []
        for item in payload.get("coins", [])[:count]:
            coin = item.get("item", {})
            trending.append({
                "id": str(coin.get("id", "")),
                "symbol": str(coin.get("symbol", "")).upper(),
                "name": str(coin.get("name", "")),
                "market_cap_rank": self._to_int(coin.get("market_cap_rank")),
            })
        return trending

    # ------------------------------------------------------------------ #
    # 5. Category/sector performance
    # ------------------------------------------------------------------ #

    def _fetch_categories(self, cat_cfg: Dict[str, Any]) -> Dict[str, Any]:
        base_url = self.cg_cfg.get("base_url", "https://api.coingecko.com/api/v3")
        sample_size = int(cat_cfg.get("sample_size", 15))
        top_g = int(cat_cfg.get("top_gainers_count", 5))
        top_l = int(cat_cfg.get("top_losers_count", 5))

        raw = self._get_json(f"{base_url}/coins/categories")
        by_cap = sorted(
            raw,
            key=lambda r: self._to_float(r.get("market_cap")),
            reverse=True,
        )[:sample_size]

        categories = [
            {
                "name": str(row.get("name", "")),
                "change_24h": self._to_float(row.get("market_cap_change_24h")),
                "market_cap": self._to_float(row.get("market_cap")),
            }
            for row in by_cap
        ]

        top_gainers = sorted(categories, key=lambda r: r["change_24h"], reverse=True)[:top_g]
        top_losers = sorted(categories, key=lambda r: r["change_24h"])[:top_l]
        return {"top_gainers": top_gainers, "top_losers": top_losers}

    # ------------------------------------------------------------------ #
    # 6. Global market data (total cap, BTC dominance)
    # ------------------------------------------------------------------ #

    def _fetch_global(self) -> Dict[str, Any]:
        base_url = self.cg_cfg.get("base_url", "https://api.coingecko.com/api/v3")
        payload = self._get_json(f"{base_url}/global")
        data = payload.get("data", {})

        market_cap_change = self._to_float(data.get("market_cap_change_percentage_24h_usd"))
        total_cap = self._to_float(data.get("total_market_cap", {}).get("usd"))
        btc_dom = self._to_float(data.get("market_cap_percentage", {}).get("btc"))
        eth_dom = self._to_float(data.get("market_cap_percentage", {}).get("eth"))
        active = self._to_int(data.get("active_cryptocurrencies"))

        return {
            "total_market_cap_usd": total_cap,
            "total_market_cap_change_24h": round(market_cap_change, 2),
            "btc_dominance": round(btc_dom, 2),
            "eth_dominance": round(eth_dom, 2),
            "active_cryptocurrencies": active,
        }

    # ------------------------------------------------------------------ #
    # 7. DexScreener
    # ------------------------------------------------------------------ #

    def _fetch_dex_pairs(self) -> List[Dict[str, Any]]:
        base_url = self.dex_cfg.get("base_url", "https://api.dexscreener.com/latest/dex")
        queries = self.dex_cfg.get("queries", [])
        top_count = int(self.dex_cfg.get("top_pairs_count", 15))

        seen: set = set()
        pairs: List[Dict[str, Any]] = []

        for query in queries:
            try:
                payload = self._get_json(f"{base_url}/search", params={"q": query})
            except Exception:
                continue

            for pair in payload.get("pairs", []):
                key = f"{pair.get('chainId', '')}:{pair.get('pairAddress', '')}"
                if key in seen:
                    continue
                seen.add(key)
                pairs.append({
                    "chain_id": str(pair.get("chainId", "")),
                    "dex_id": str(pair.get("dexId", "")),
                    "pair_address": str(pair.get("pairAddress", "")),
                    "base_symbol": str(pair.get("baseToken", {}).get("symbol", "")),
                    "quote_symbol": str(pair.get("quoteToken", {}).get("symbol", "")),
                    "price_usd": self._to_float(pair.get("priceUsd")),
                    "volume_24h": self._to_float(pair.get("volume", {}).get("h24")),
                    "liquidity_usd": self._to_float(pair.get("liquidity", {}).get("usd")),
                    "change_24h": self._to_float(pair.get("priceChange", {}).get("h24")),
                })

        pairs.sort(key=lambda r: r["volume_24h"], reverse=True)
        return pairs[:top_count]

    # ------------------------------------------------------------------ #
    # 8. Fear & Greed
    # ------------------------------------------------------------------ #

    def _fetch_sentiment(self) -> Dict[str, Any]:
        url = self.fg_cfg.get("url", "https://api.alternative.me/fng/?limit=1&format=json")
        payload = self._get_json(url)
        row = payload.get("data", [{}])[0]
        index_val = self._to_int(row.get("value"))

        # Classification thresholds from YAML
        extreme_fear_max = int(self.fg_cfg.get("extreme_fear_max", 25))
        fear_max = int(self.fg_cfg.get("fear_max", 45))
        neutral_max = int(self.fg_cfg.get("neutral_max", 55))
        greed_max = int(self.fg_cfg.get("greed_max", 75))

        if index_val <= extreme_fear_max:
            classification = "extreme_fear"
        elif index_val <= fear_max:
            classification = "fear"
        elif index_val <= neutral_max:
            classification = "neutral"
        elif index_val <= greed_max:
            classification = "greed"
        else:
            classification = "extreme_greed"

        return {"fear_greed_index": index_val, "classification": classification}

    # ------------------------------------------------------------------ #
    # 9. Stablecoin flow proxy (CoinGecko)
    # ------------------------------------------------------------------ #

    def _fetch_stablecoin(self) -> Dict[str, Any]:
        """Fetch USDT + USDC market caps to gauge stablecoin inflow/outflow."""
        base_url = self.cg_cfg.get("base_url", "https://api.coingecko.com/api/v3")
        ids = self.stablecoin_cfg.get("ids", "tether,usd-coin")

        payload = self._get_json(
            f"{base_url}/simple/price",
            params={
                "ids": ids,
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_change": "true",
            },
        )

        usdt = payload.get("tether", {})
        usdc = payload.get("usd-coin", {})

        usdt_mcap = self._to_float(usdt.get("usd_market_cap"))
        usdc_mcap = self._to_float(usdc.get("usd_market_cap"))
        total_mcap = usdt_mcap + usdc_mcap

        usdt_change = self._to_float(usdt.get("usd_24h_change"))
        usdc_change = self._to_float(usdc.get("usd_24h_change"))

        # Weighted average of 24h change by market cap
        if total_mcap > 0:
            change_24h = (usdt_change * usdt_mcap + usdc_change * usdc_mcap) / total_mcap
        else:
            change_24h = 0.0

        # Determine flow status
        if change_24h > 0.5:
            flow_status = "inflow"
        elif change_24h < -0.5:
            flow_status = "outflow"
        else:
            flow_status = "neutral"

        return {
            "usdt_market_cap": round(usdt_mcap, 2),
            "usdc_market_cap": round(usdc_mcap, 2),
            "total_mcap": round(total_mcap, 2),
            "change_24h_pct": round(change_24h, 4),
            "flow_status": flow_status,
        }

    # ------------------------------------------------------------------ #
    # 10. Macro correlations (yfinance)
    # ------------------------------------------------------------------ #

    def _fetch_macro(self) -> Dict[str, Any]:
        """Fetch S&P 500, VIX, DXY from yfinance for macro correlation."""
        if not _HAS_YFINANCE:
            raise ImportError("yfinance not installed — macro data skipped")

        tickers_cfg = self.macro_cfg.get("tickers", {})
        sp500_ticker = tickers_cfg.get("sp500", "^GSPC")
        vix_ticker = tickers_cfg.get("vix", "^VIX")
        dxy_ticker = tickers_cfg.get("dxy", "DX-Y.NYB")

        result: Dict[str, Any] = {
            "sp500_price": None,
            "sp500_change_1d_pct": None,
            "vix_level": None,
            "vix_status": "unknown",
            "dxy_price": None,
            "dxy_change_1d_pct": None,
            "risk_status": "unknown",
        }

        # Fetch S&P 500
        try:
            sp = yf.Ticker(sp500_ticker)
            hist = sp.history(period="5d")
            if len(hist) >= 2:
                result["sp500_price"] = round(float(hist["Close"].iloc[-1]), 2)
                prev = float(hist["Close"].iloc[-2])
                if prev > 0:
                    result["sp500_change_1d_pct"] = round(
                        (result["sp500_price"] - prev) / prev * 100, 2
                    )
        except Exception:
            pass

        # Fetch VIX
        try:
            vx = yf.Ticker(vix_ticker)
            hist = vx.history(period="5d")
            if len(hist) >= 1:
                vix_val = round(float(hist["Close"].iloc[-1]), 2)
                result["vix_level"] = vix_val
                if vix_val > 30:
                    result["vix_status"] = "extreme_fear"
                elif vix_val > 20:
                    result["vix_status"] = "elevated"
                else:
                    result["vix_status"] = "calm"
        except Exception:
            pass

        # Fetch DXY
        try:
            dx = yf.Ticker(dxy_ticker)
            hist = dx.history(period="5d")
            if len(hist) >= 2:
                result["dxy_price"] = round(float(hist["Close"].iloc[-1]), 2)
                prev = float(hist["Close"].iloc[-2])
                if prev > 0:
                    result["dxy_change_1d_pct"] = round(
                        (result["dxy_price"] - prev) / prev * 100, 2
                    )
        except Exception:
            pass

        # Determine macro risk status
        sp_chg = result["sp500_change_1d_pct"]
        vix_lvl = result["vix_level"]
        dxy_chg = result["dxy_change_1d_pct"]

        if sp_chg is not None and vix_lvl is not None and dxy_chg is not None:
            if vix_lvl > 25 or sp_chg < -1.5 or dxy_chg > 0.5:
                result["risk_status"] = "risk_off"
            elif vix_lvl < 18 and sp_chg > 0.5 and dxy_chg < -0.3:
                result["risk_status"] = "risk_on"
            else:
                result["risk_status"] = "neutral"

        return result

    # ------------------------------------------------------------------ #
    # Market score computation (per-asset, 0-100)
    # ------------------------------------------------------------------ #

    def _compute_market_scores(self, data: Dict[str, Any]) -> None:
        """Compute a 0-100 market_score for each asset using weighted components."""
        weights_cfg = self.scoring_cfg.get("weights", {})
        w_fg = float(weights_cfg.get("fear_greed", 0.25))
        w_vol = float(weights_cfg.get("volume", 0.15))
        w_breadth = float(weights_cfg.get("breadth", 0.15))
        w_macro = float(weights_cfg.get("macro", 0.20))
        w_ob = float(weights_cfg.get("order_book", 0.25))

        # --- Fear & Greed score (same for all assets) ---
        fg_index = data.get("sentiment", {}).get("fear_greed_index")
        fg_classification = data.get("sentiment", {}).get("classification", "neutral")
        fg_score = self._fg_to_score(fg_index, fg_classification)

        # --- Macro score (same for all assets) ---
        risk_status = data.get("macro", {}).get("risk_status", "unknown")
        macro_score = self._macro_to_score(risk_status)

        # --- Breadth: build set of gainer/loser symbols ---
        gainer_syms = set()
        loser_syms = set()
        for coin in data.get("breadth", {}).get("top_gainers", []):
            gainer_syms.add(coin.get("symbol", "").upper())
        for coin in data.get("breadth", {}).get("top_losers", []):
            loser_syms.add(coin.get("symbol", "").upper())

        # --- Per-asset scoring ---
        for sym, info in data.get("per_asset", {}).items():
            # Volume score
            vol_status = info.get("volume_status", "unknown")
            ratio = info.get("volume_spike_ratio")
            vol_score = self._volume_to_score(vol_status, ratio)

            # Breadth score (wider range)
            if sym in gainer_syms:
                breadth_score = 75.0
            elif sym in loser_syms:
                breadth_score = 25.0
            else:
                breadth_score = 50.0

            # Order book score
            imbalance = info.get("order_book_imbalance")
            ob_score = self._ob_to_score(imbalance)

            # Weighted average
            score = (
                fg_score * w_fg
                + vol_score * w_vol
                + breadth_score * w_breadth
                + macro_score * w_macro
                + ob_score * w_ob
            )
            score = max(0.0, min(100.0, score))
            info["market_score"] = round(score, 1)

    @staticmethod
    def _fg_to_score(index_val: int | None, classification: str) -> float:
        """Convert Fear & Greed index to a contrarian score (wider range)."""
        if index_val is None:
            return 50.0
        fg = float(index_val)
        # Continuous contrarian mapping: low F&G = bullish opportunity, high = bearish
        # F&G 0 → 90 (extreme fear = max bullish), F&G 50 → 50 (neutral)
        # F&G 100 → 10 (extreme greed = max bearish)
        score = 90.0 - (fg / 100.0) * 80.0  # 0→90, 50→50, 100→10
        return max(10.0, min(90.0, score))

    @staticmethod
    def _volume_to_score(vol_status: str, ratio: float | None) -> float:
        """Convert volume status to score (continuous based on ratio)."""
        if ratio is not None:
            # ratio = 24h_vol / 7d_avg. >1 = above average
            if ratio > 3.0:
                return 85.0  # massive spike
            elif ratio > 2.0:
                return 70.0 + (ratio - 2.0) * 15.0  # 70→85
            elif ratio > 1.0:
                return 55.0 + (ratio - 1.0) * 15.0  # 55→70
            elif ratio > 0.5:
                return 35.0 + (ratio - 0.5) * 40.0  # 35→55
            else:
                return 20.0 + ratio * 30.0  # 20→35
        # Fallback to status
        if vol_status == "spike":
            return 75.0
        elif vol_status == "elevated":
            return 60.0
        return 50.0

    @staticmethod
    def _macro_to_score(risk_status: str) -> float:
        """Convert macro risk status to score (wider range)."""
        if risk_status == "strong_risk_on":
            return 80.0
        elif risk_status == "risk_on":
            return 70.0
        elif risk_status == "risk_off":
            return 30.0
        elif risk_status == "strong_risk_off":
            return 20.0
        return 50.0  # neutral or unknown

    @staticmethod
    def _ob_to_score(imbalance: float | None) -> float:
        """Convert order book imbalance to score (continuous, capped range).

        Order book snapshots are noisy — cap the range to avoid extreme
        scores from momentary imbalances.
        """
        if imbalance is None:
            return 50.0
        # imbalance = bid_vol / ask_vol. >1 = more bids = bullish
        # Capped to 30-70 range (order book is noisy, shouldn't dominate)
        if imbalance >= 1.0:
            intensity = min((imbalance - 1.0) / 1.5, 1.0)
            return 50.0 + intensity * 20.0  # 50→70
        else:
            intensity = min((1.0 - imbalance) / 0.7, 1.0)
            return 50.0 - intensity * 20.0  # 50→30

    # ------------------------------------------------------------------ #
    # Summary builder
    # ------------------------------------------------------------------ #

    def _build_summary(self, data: Dict[str, Any]) -> Dict[str, Any]:
        spike_assets = []
        elevated_assets = []
        top_gainer = None
        top_loser = None
        best_change = -999.0
        worst_change = 999.0

        for sym, info in data.get("per_asset", {}).items():
            vol_status = info.get("volume_status", "unknown")
            if vol_status == "spike":
                spike_assets.append(sym)
            elif vol_status == "elevated":
                elevated_assets.append(sym)

            change = info.get("change_24h_pct", 0.0) or 0.0
            if change > best_change:
                best_change = change
                top_gainer = sym
            if change < worst_change:
                worst_change = change
                top_loser = sym

        # Market direction from global data
        global_change = data.get("global_market", {}).get("total_market_cap_change_24h")
        if global_change is not None:
            if global_change > 1.0:
                market_direction = "bullish"
            elif global_change < -1.0:
                market_direction = "bearish"
            else:
                market_direction = "neutral"
        else:
            market_direction = "unknown"

        return {
            "volume_spike_assets": spike_assets,
            "elevated_volume_assets": elevated_assets,
            "top_gainer_asset": top_gainer,
            "top_loser_asset": top_loser,
            "market_direction": market_direction,
        }

    # ------------------------------------------------------------------ #
    # HTTP + helpers
    # ------------------------------------------------------------------ #

    def _get_json(self, url: str, params: Dict[str, Any] | None = None) -> Any:
        full_url = f"{url}?{urlencode(params)}" if params else url
        req = Request(full_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _normalize_coin(coin: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(coin.get("id", "")),
            "symbol": str(coin.get("symbol", "")).upper(),
            "name": str(coin.get("name", "")),
            "price": MarketAgent._to_float(coin.get("current_price")),
            "change_24h_pct": MarketAgent._to_float(coin.get("price_change_percentage_24h")),
            "market_cap": MarketAgent._to_float(coin.get("market_cap")),
            "volume_24h": MarketAgent._to_float(coin.get("total_volume")),
        }

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0
