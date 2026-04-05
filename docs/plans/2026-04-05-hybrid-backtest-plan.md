# Hybrid Backtest & Per-Asset Weight Optimization — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace hardcoded weights with per-asset, backtest-derived weights. Add missing leading indicators. Build deploy gate that blocks untested changes.

**Architecture:** 3-agent system (technical, derivatives, market). Two-phase backtest: Phase 1 uses 6 months of historical klines+macro for IC analysis, Phase 2 uses 90 days of live agent data for weight optimization. Grid search over ~66 weight combos per asset. Walk-forward validation with 7-day embargo.

**Tech Stack:** Python 3.11+, pytest, numpy, yfinance, requests (Binance API), scipy (Spearman IC)

**Design Doc:** `docs/plans/2026-04-05-hybrid-backtest-design.md`

---

## Task 1: Cut Dead Agents (Narrative + Exchange Flow)

**Files:**
- Modify: `scoring/pipeline.py:28-35` — Remove from ALL_DIMENSIONS and SCORE_FNS
- Modify: `scoring/dimensions.py:279-298` — Delete score_narrative() and score_exchange_flow()
- Modify: `config.yaml:1-69` — Remove narrative/exchange_flow from all weight dicts
- Modify: `config.yaml:145-172` — Remove narrative/exchange_flow agent config sections
- Modify: `orchestrator/runner.py:23-60` — Remove narrative/exchange_flow from _load_agents()
- Modify: `agents/__init__.py` — Remove narrative/exchange_flow imports
- Delete: `agents/narrative.py`
- Delete: `agents/exchange_flow.py`
- Test: `tests/test_pipeline.py`, `tests/test_dimensions.py`, `tests/test_config.py`

**Step 1: Update tests to reflect 3-agent system**

In `tests/test_dimensions.py`, remove any tests for `score_narrative` and `score_exchange_flow`. In `tests/test_pipeline.py`, update `ALL_DIMENSIONS` expectations to only include `["technical", "derivatives", "market"]`. In `tests/test_config.py`, update weight sum validation to only require 3 dimensions.

**Step 2: Run tests to verify they fail**

```bash
cd /Users/admin/Documents/web3-signals && python3 -m pytest tests/test_pipeline.py tests/test_dimensions.py tests/test_config.py -v
```

Expected: FAIL — tests expect 3 dimensions but code still has 5.

**Step 3: Remove from scoring pipeline**

In `scoring/pipeline.py` line 28:
```python
# Before
ALL_DIMENSIONS = ["technical", "derivatives", "market", "narrative", "exchange_flow"]

# After
ALL_DIMENSIONS = ["technical", "derivatives", "market"]
```

Remove `score_narrative` and `score_exchange_flow` from SCORE_FNS dict (lines 29-35).

Remove the imports of `score_narrative` and `score_exchange_flow` from the top of the file.

**Step 4: Remove scoring functions**

In `scoring/dimensions.py`, delete `score_narrative()` (lines 279-286) and `score_exchange_flow()` (lines 291-298).

**Step 5: Clean config.yaml**

Remove `narrative: 0.00` and `exchange_flow: 0.00` from `weights_default`, `weights_bullish`, `weights_bearish`, and all `per_tier_weights` sections. Renormalize remaining weights to sum to 1.0:

```yaml
weights_default:
  technical: 0.45
  derivatives: 0.05
  market: 0.50

weights_bullish:
  technical: 0.40
  derivatives: 0.05
  market: 0.55

weights_bearish:
  technical: 0.50
  derivatives: 0.05
  market: 0.45
```

Remove the `narrative:` and `exchange_flow:` agent config sections (lines ~145-172).

Remove narrative and exchange_flow from regime `weight_shifts`.

**Step 6: Remove from orchestrator**

In `orchestrator/runner.py` `_load_agents()` (lines 23-60), remove the try/except blocks that load `NarrativeAgent` and `ExchangeFlowAgent`.

**Step 7: Delete agent files**

```bash
rm /Users/admin/Documents/web3-signals/agents/narrative.py
rm /Users/admin/Documents/web3-signals/agents/exchange_flow.py
```

Remove imports from `agents/__init__.py` if present.

**Step 8: Run tests to verify they pass**

```bash
python3 -m pytest tests/ -v
```

Expected: ALL PASS. The system now has exactly 3 dimensions.

**Step 9: Verify signals still generate**

```bash
python3 -c "
from scoring.config import load_config
from scoring.pipeline import fuse_signals, ALL_DIMENSIONS
print('Dimensions:', ALL_DIMENSIONS)
cfg = load_config()
print('Weights sum:', sum(cfg.scoring.weights_default.values()))
print('OK')
"
```

**Step 10: Commit**

```bash
git add -A && git commit -m "refactor: cut dead agents (narrative + exchange_flow) — 3-agent system"
```

---

## Task 2: Fix Derivatives Agent — OI Change %

**Files:**
- Modify: `agents/derivatives.py:28-87` — Track previous OI, compute actual change
- Modify: `storage/db.py` — Add method to load previous OI value
- Test: `tests/test_agents.py`

**Step 1: Write failing test**

In `tests/test_agents.py`, add:

```python
def test_derivatives_oi_change_not_hardcoded():
    """OI change % must be computed, not hardcoded to 0.0."""
    # Mock two consecutive calls with different OI values
    result = mock_derivatives_result(oi_current=50000000, oi_previous=48000000)
    assert result["oi_change_pct"] != 0.0
    assert abs(result["oi_change_pct"] - 4.17) < 0.5  # (50M-48M)/48M * 100
```

**Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_agents.py::test_derivatives_oi_change_not_hardcoded -v
```

**Step 3: Implement OI change tracking**

In `agents/derivatives.py`, after fetching current OI from Binance:

```python
# Load previous OI from storage (or use current if first run)
prev_oi = storage.load_kv(f"prev_oi_{symbol}") or current_oi
oi_change_pct = ((current_oi - prev_oi) / prev_oi * 100) if prev_oi > 0 else 0.0
storage.save_kv(f"prev_oi_{symbol}", str(current_oi))
```

Replace the hardcoded `"oi_change_pct": 0.0` with the computed value.

**Step 4: Run tests**

```bash
python3 -m pytest tests/test_agents.py -v
```

**Step 5: Commit**

```bash
git add agents/derivatives.py storage/db.py tests/test_agents.py
git commit -m "fix: compute actual OI change % instead of hardcoded 0.0"
```

---

## Task 3: Fix Market Agent — Fetch S&P, DXY, NASDAQ

**Files:**
- Modify: `agents/market.py:27-89` — Actually fetch macro data via yfinance
- Test: `tests/test_agents.py`

**Step 1: Write failing test**

```python
def test_market_agent_fetches_macro_data():
    """Market agent must return real macro data, not nulls."""
    result = run_market_agent_with_mock()
    assert result.get("sp500_change") is not None
    assert result.get("dxy_change") is not None
    assert result.get("nasdaq_change") is not None
    assert isinstance(result["sp500_change"], (int, float))
```

**Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_agents.py::test_market_agent_fetches_macro_data -v
```

**Step 3: Implement macro data fetching**

In `agents/market.py`, add a `_fetch_macro()` method:

```python
import yfinance as yf

def _fetch_macro(self):
    """Fetch S&P 500, DXY, NASDAQ daily change."""
    macro = {}
    for ticker, key in [("SPY", "sp500_change"), ("DX-Y.NYB", "dxy_change"), ("QQQ", "nasdaq_change")]:
        try:
            data = yf.download(ticker, period="5d", interval="1d", progress=False)
            if len(data) >= 2:
                macro[key] = float((data["Close"].iloc[-1] - data["Close"].iloc[-2]) / data["Close"].iloc[-2] * 100)
            else:
                macro[key] = 0.0
        except Exception:
            macro[key] = 0.0
    return macro
```

Call `_fetch_macro()` in `collect()` and merge results. Replace hardcoded `breadth_status: "neutral"` with computed BTC dominance from CoinGecko `/global`.

**Step 4: Run tests**

```bash
python3 -m pytest tests/test_agents.py -v
```

**Step 5: Commit**

```bash
git add agents/market.py tests/test_agents.py
git commit -m "fix: market agent fetches real S&P, DXY, NASDAQ data via yfinance"
```

---

## Task 4: Add Technical Indicators — OBV + MFI

**Files:**
- Modify: `agents/technical.py:29-92` — Add OBV and MFI computation
- Modify: `scoring/dimensions.py:74-112` — Use OBV/MFI in score_technical()
- Test: `tests/test_dimensions.py`

**Step 1: Write failing test**

```python
def test_technical_score_includes_obv_mfi():
    """Technical scoring must use OBV and MFI when provided."""
    data = {
        "rsi": 45, "macd_histogram": 0.5, "bb_position": 0.4,
        "price": 84000, "ma_7": 83500, "ma_30": 82000,
        "volume_status": "normal",
        "obv_slope": 0.05,  # Positive OBV slope = bullish
        "mfi": 35,  # Below 40 = oversold = bullish
    }
    result = score_technical(data, tech_config)
    # OBV bullish + MFI oversold should boost score above what we'd get without them
    assert result.score > 55
```

**Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_dimensions.py::test_technical_score_includes_obv_mfi -v
```

**Step 3: Add OBV computation in technical agent**

In `agents/technical.py`, add after existing indicator computation:

```python
# OBV (On-Balance Volume)
obv = 0
for i in range(1, len(closes)):
    if closes[i] > closes[i-1]:
        obv += volumes[i]
    elif closes[i] < closes[i-1]:
        obv -= volumes[i]
obv_values.append(obv)

# OBV slope (normalized rate of change over last 5 periods)
obv_slope = (obv_values[-1] - obv_values[-6]) / abs(obv_values[-6]) if len(obv_values) >= 6 and obv_values[-6] != 0 else 0

# MFI (Money Flow Index, 14-period)
typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
raw_money_flow = [tp * v for tp, v in zip(typical_prices, volumes)]
positive_flow = sum(raw_money_flow[i] for i in range(-14, 0) if typical_prices[i] > typical_prices[i-1])
negative_flow = sum(raw_money_flow[i] for i in range(-14, 0) if typical_prices[i] < typical_prices[i-1])
mfi = 100 - (100 / (1 + positive_flow / negative_flow)) if negative_flow > 0 else 50
```

Add `obv_slope` and `mfi` to the returned data dict.

**Step 4: Add OBV/MFI scoring in dimensions.py**

In `scoring/dimensions.py` `score_technical()`, add:

```python
# OBV score: positive slope = bullish
obv_slope = data.get("obv_slope", 0)
obv_score = 50 + min(max(obv_slope * 500, -40), 40)  # Scale to 10-90

# MFI score: <20 = very oversold (bullish), >80 = very overbought (bearish)
mfi = data.get("mfi", 50)
mfi_score = 90 - (mfi / 100) * 80  # Similar to RSI scoring
```

Update weights: RSI 15%, MACD 20%, Bollinger 15%, Trend 20%, OBV 15%, MFI 15%.

**Step 5: Run tests**

```bash
python3 -m pytest tests/ -v
```

**Step 6: Commit**

```bash
git add agents/technical.py scoring/dimensions.py tests/test_dimensions.py
git commit -m "feat: add OBV and MFI to technical agent and scoring"
```

---

## Task 5: Add Technical Indicators — ROC, StochRSI, Squeeze, Z-scores

**Files:**
- Modify: `agents/technical.py` — Add ROC(1d, 7d, 30d), StochRSI, BB/Keltner squeeze, z-scores
- Test: `tests/test_dimensions.py`

**Step 1: Write failing test**

```python
def test_technical_agent_computes_advanced_indicators():
    """Technical agent must return ROC, StochRSI, squeeze, z-scores."""
    result = run_technical_agent_mock(candles_180d)
    assert "roc_1d" in result
    assert "roc_7d" in result
    assert "roc_30d" in result
    assert "stoch_rsi" in result
    assert "squeeze_on" in result
    assert "rsi_zscore" in result
    assert "macd_zscore" in result
```

**Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_dimensions.py::test_technical_agent_computes_advanced_indicators -v
```

**Step 3: Implement in technical agent**

In `agents/technical.py`, add after existing indicators:

```python
# ROC (Rate of Change) at 3 periods
roc_1d = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
roc_7d = (closes[-1] - closes[-8]) / closes[-8] * 100 if len(closes) >= 8 else 0
roc_30d = (closes[-1] - closes[-31]) / closes[-31] * 100 if len(closes) >= 31 else 0

# Stochastic RSI
rsi_values = [compute_rsi(closes[:i+1], 14) for i in range(13, len(closes))]
rsi_min = min(rsi_values[-14:])
rsi_max = max(rsi_values[-14:])
stoch_rsi = (rsi_values[-1] - rsi_min) / (rsi_max - rsi_min) if rsi_max != rsi_min else 0.5

# BB/Keltner Squeeze
bb_upper = sma_20 + 2 * std_20
bb_lower = sma_20 - 2 * std_20
atr_20 = compute_atr(highs, lows, closes, 20)
kc_upper = sma_20 + 1.5 * atr_20
kc_lower = sma_20 - 1.5 * atr_20
squeeze_on = bb_lower > kc_lower and bb_upper < kc_upper

# Z-scores (50-period rolling)
rsi_zscore = (rsi - np.mean(rsi_values[-50:])) / np.std(rsi_values[-50:]) if len(rsi_values) >= 50 else 0
macd_zscore = (macd_hist - np.mean(macd_hist_values[-50:])) / np.std(macd_hist_values[-50:]) if len(macd_hist_values) >= 50 else 0
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/ -v
```

**Step 5: Commit**

```bash
git add agents/technical.py tests/test_dimensions.py
git commit -m "feat: add ROC, StochRSI, BB/Keltner squeeze, z-scores to technical agent"
```

---

## Task 6: Add Market Sources — Stablecoin Supply, BTC Dominance, VIX ROC

**Files:**
- Modify: `agents/market.py` — Add stablecoin supply (DefiLlama), BTC dominance (CoinGecko), VIX ROC
- Modify: `scoring/dimensions.py:248-274` — Update score_market() to use new data
- Test: `tests/test_dimensions.py`

**Step 1: Write failing test**

```python
def test_market_score_includes_new_sources():
    """Market scoring must use stablecoin supply, BTC dominance, VIX ROC."""
    data = {
        "fear_greed": 45, "volume_ratio": 1.2, "breadth_status": "neutral",
        "macro_status": "neutral", "order_book_imbalance": 1.1,
        "stablecoin_supply_change_7d": 2.5,  # Growing = bullish
        "btc_dominance": 58.0,  # High = bearish for alts
        "vix_roc": -5.0,  # VIX falling = risk-on = bullish
        "nasdaq_change": 1.2,  # NASDAQ up = risk-on
    }
    result = score_market(data, market_config)
    assert result.score > 50  # Net bullish data
```

**Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_dimensions.py::test_market_score_includes_new_sources -v
```

**Step 3: Implement data fetching**

In `agents/market.py`:

```python
# Stablecoin supply from DefiLlama
def _fetch_stablecoin_supply(self):
    try:
        resp = requests.get("https://stablecoins.llama.fi/stablecoins?includePrices=false", timeout=10)
        data = resp.json()
        total_now = sum(s.get("circulating", {}).get("peggedUSD", 0) for s in data.get("peggedAssets", [])[:5])
        # Compare to 7d ago from historical
        return {"stablecoin_supply_change_7d": computed_7d_change}
    except Exception:
        return {"stablecoin_supply_change_7d": 0.0}

# BTC dominance from CoinGecko
def _fetch_btc_dominance(self):
    try:
        resp = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        data = resp.json()["data"]
        return {"btc_dominance": data["market_cap_percentage"]["btc"]}
    except Exception:
        return {"btc_dominance": 50.0}
```

VIX ROC: compute from existing VIX fetch (current VIX vs previous VIX).

**Step 4: Update market scoring**

In `scoring/dimensions.py` `score_market()`, add:

```python
# Stablecoin supply growth: positive = money entering = bullish
stable_growth = data.get("stablecoin_supply_change_7d", 0)
stable_score = 50 + min(max(stable_growth * 5, -30), 30)

# BTC dominance (for alts): rising dominance = bearish for alts
btc_dom = data.get("btc_dominance", 50)
# Only applied per-asset in pipeline — for now just pass through

# VIX ROC: falling VIX = risk-on = bullish
vix_roc = data.get("vix_roc", 0)
vix_score = 50 + min(max(-vix_roc * 3, -25), 25)

# NASDAQ correlation
nasdaq_change = data.get("nasdaq_change", 0)
nasdaq_score = 50 + min(max(nasdaq_change * 10, -30), 30)
```

Updated weights: F&G 20%, Volume 10%, Macro 15%, Order Book 20%, Stablecoin 15%, VIX 10%, NASDAQ 10%.

**Step 5: Run tests**

```bash
python3 -m pytest tests/ -v
```

**Step 6: Commit**

```bash
git add agents/market.py scoring/dimensions.py tests/test_dimensions.py
git commit -m "feat: add stablecoin supply, BTC dominance, VIX ROC, NASDAQ to market agent"
```

---

## Task 7: Add OI-Weighted Funding + Relative Features

**Files:**
- Modify: `agents/derivatives.py` — Add OI-weighted funding rate
- Modify: `scoring/pipeline.py` — Add relative features (asset vs BTC) between Step 1 and Step 4
- Test: `tests/test_pipeline.py`

**Step 1: Write failing test**

```python
def test_pipeline_computes_relative_features():
    """Pipeline must compute asset-vs-BTC relative features."""
    agent_data = make_multi_asset_data(btc_rsi=55, eth_rsi=70)
    signals = fuse_signals(agent_data, cfg, assets_cfg)
    # ETH's relative momentum should be positive (70 - 55 = 15)
    eth_signal = signals["ETH"]
    assert eth_signal.dimensions["technical"].score != signals["BTC"].dimensions["technical"].score
```

**Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_pipeline.py::test_pipeline_computes_relative_features -v
```

**Step 3: Add OI-weighted funding**

In `agents/derivatives.py`:

```python
oi_weighted_funding = funding_rate * open_interest
```

Add to returned data dict.

**Step 4: Add relative features in pipeline**

In `scoring/pipeline.py`, after Step 1 (dimension scoring) and before Step 4 (composite):

```python
# Relative features: asset vs BTC
btc_data = agent_data.get("BTC", {})
if asset != "BTC" and btc_data:
    btc_tech = btc_data.get("technical_agent", {})
    asset_tech = asset_data.get("technical_agent", {})

    relative_momentum = asset_tech.get("rsi", 50) - btc_tech.get("rsi", 50)
    relative_strength = asset_tech.get("roc_1d", 0) - btc_tech.get("roc_1d", 0)

    # Adjust technical score by relative momentum
    rel_adjustment = min(max(relative_momentum * 0.1, -5), 5)
    dimensions["technical"] = dimensions["technical"]._replace(
        score=max(0, min(100, dimensions["technical"].score + rel_adjustment))
    )
```

**Step 5: Run tests**

```bash
python3 -m pytest tests/ -v
```

**Step 6: Commit**

```bash
git add agents/derivatives.py scoring/pipeline.py tests/test_pipeline.py
git commit -m "feat: add OI-weighted funding and relative features (asset vs BTC)"
```

---

## Task 8: Build Historical Data Fetcher (Phase 1)

**Files:**
- Create: `tools/historical_fetcher.py` — Fetch 180 days of klines + macro data
- Test: `tests/test_historical_fetcher.py`

**Step 1: Write failing test**

```python
def test_historical_fetcher_returns_180_days():
    """Fetcher must return at least 180 days of daily candles for BTC."""
    from tools.historical_fetcher import fetch_historical_klines
    candles = fetch_historical_klines("BTCUSDT", days=180)
    assert len(candles) >= 170  # Allow some tolerance
    assert "open" in candles[0]
    assert "close" in candles[0]
    assert "volume" in candles[0]
    assert "high" in candles[0]
    assert "low" in candles[0]
    assert "timestamp" in candles[0]
```

**Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_historical_fetcher.py -v
```

**Step 3: Implement historical fetcher**

Create `tools/historical_fetcher.py`:

```python
"""Fetch historical data for backtesting. No future data leakage possible —
all data is timestamped and fetched from exchange historical endpoints."""

import requests
import time
from datetime import datetime, timedelta

def fetch_historical_klines(symbol: str, days: int = 180, interval: str = "1d") -> list[dict]:
    """Fetch daily klines from Binance. Returns list of OHLCV dicts."""
    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)

    all_candles = []
    while start_ms < end_ms:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&startTime={start_ms}&limit=1000"
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if not data:
            break
        for c in data:
            all_candles.append({
                "timestamp": c[0],
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            })
        start_ms = data[-1][0] + 1
        time.sleep(0.1)  # Rate limit
    return all_candles

def fetch_historical_macro(days: int = 180) -> dict:
    """Fetch S&P, DXY, NASDAQ, VIX historical via yfinance."""
    import yfinance as yf
    macro = {}
    for ticker, key in [("SPY", "sp500"), ("DX-Y.NYB", "dxy"), ("QQQ", "nasdaq"), ("^VIX", "vix")]:
        try:
            data = yf.download(ticker, period=f"{days}d", interval="1d", progress=False)
            macro[key] = [{"date": str(d.date()), "close": float(row["Close"])} for d, row in data.iterrows()]
        except Exception:
            macro[key] = []
    return macro

def fetch_historical_fear_greed(days: int = 180) -> list[dict]:
    """Fetch Fear & Greed index history."""
    try:
        resp = requests.get(f"https://api.alternative.me/fng/?limit={days}", timeout=10)
        return [{"date": d["timestamp"], "value": int(d["value"])} for d in resp.json()["data"]]
    except Exception:
        return []
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/test_historical_fetcher.py -v
```

Note: This test hits real APIs. For CI, mock the HTTP calls.

**Step 5: Commit**

```bash
git add tools/historical_fetcher.py tests/test_historical_fetcher.py
git commit -m "feat: historical data fetcher for Phase 1 backtest (klines + macro)"
```

---

## Task 9: Build Walk-Forward Backtest Engine

**Files:**
- Create: `tools/walk_forward.py` — Walk-forward validation with embargo
- Modify: `tools/backtest.py` — Integrate walk-forward as the primary mode
- Create: `tests/test_walk_forward.py`

**Step 1: Write failing tests**

```python
def test_walk_forward_no_future_leakage():
    """Train window must end before embargo, test must start after embargo."""
    folds = generate_folds(total_days=180, embargo_days=7, test_window=21, min_train=90)
    for fold in folds:
        assert fold["train_end"] + 7 <= fold["test_start"]
        assert fold["train_start"] == 0  # Expanding window

def test_walk_forward_expanding_window():
    """Each fold's training window must be strictly larger than the previous."""
    folds = generate_folds(total_days=180, embargo_days=7, test_window=21, min_train=90)
    for i in range(1, len(folds)):
        assert folds[i]["train_end"] > folds[i-1]["train_end"]

def test_gradient_score_buy_up():
    """BUY signal + price went up strongly → score 1.0."""
    score = gradient_score("bullish", 4.5, noise_threshold=1.0, strong_threshold=3.0)
    assert score == 1.0

def test_gradient_score_buy_down():
    """BUY signal + price went down → score 0.0."""
    score = gradient_score("bullish", -2.0, noise_threshold=1.0, strong_threshold=3.0)
    assert score == 0.0

def test_gradient_score_neutral_stays_flat():
    """NEUTRAL signal + price stayed flat → score 1.0."""
    score = evaluate_neutral(actual_pct=0.5, atr_band_pct=1.5)
    assert score == 1.0

def test_gradient_score_neutral_missed_move():
    """NEUTRAL signal + price moved a lot → score 0.0 (abstain miss)."""
    score = evaluate_neutral(actual_pct=5.0, atr_band_pct=1.5)
    assert score == 0.0
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_walk_forward.py -v
```

**Step 3: Implement walk-forward engine**

Create `tools/walk_forward.py`:

```python
"""Walk-forward backtest engine with embargo and data leakage protection."""

from dataclasses import dataclass
import numpy as np

@dataclass
class Fold:
    train_start: int  # Day index
    train_end: int
    embargo_start: int
    embargo_end: int
    test_start: int
    test_end: int

def generate_folds(total_days: int, embargo_days: int = 7,
                   test_window: int = 21, min_train: int = 90) -> list[Fold]:
    """Generate expanding-window walk-forward folds."""
    folds = []
    test_start = min_train + embargo_days
    while test_start + test_window <= total_days:
        fold = Fold(
            train_start=0,
            train_end=test_start - embargo_days,
            embargo_start=test_start - embargo_days,
            embargo_end=test_start,
            test_start=test_start,
            test_end=min(test_start + test_window, total_days),
        )
        folds.append(fold)
        test_start += test_window
    return folds

def gradient_score(direction: str, actual_pct_change: float,
                   noise_threshold: float, strong_threshold: float) -> float:
    """Score a directional prediction against actual price change."""
    if direction == "bullish":
        if actual_pct_change >= strong_threshold:
            return 1.0
        elif actual_pct_change >= noise_threshold:
            return 0.7
        elif actual_pct_change > 0:
            return 0.4
        else:
            return 0.0
    elif direction == "bearish":
        if actual_pct_change <= -strong_threshold:
            return 1.0
        elif actual_pct_change <= -noise_threshold:
            return 0.7
        elif actual_pct_change < 0:
            return 0.4
        else:
            return 0.0
    return 0.5  # Neutral handled separately

def evaluate_neutral(actual_pct: float, atr_band_pct: float) -> float:
    """Evaluate a neutral/abstain signal. Correct if price stayed in band."""
    return 1.0 if abs(actual_pct) <= atr_band_pct else 0.0

def compute_cwa(correct: int, total: int, directional: int,
                target_coverage: float = 0.30) -> float:
    """Coverage-Weighted Accuracy. Penalizes low coverage."""
    if total == 0:
        return 0.0
    accuracy = correct / total
    coverage = directional / total
    coverage_factor = min(coverage / target_coverage, 1.0)
    return accuracy * coverage_factor
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/test_walk_forward.py -v
```

**Step 5: Commit**

```bash
git add tools/walk_forward.py tests/test_walk_forward.py
git commit -m "feat: walk-forward backtest engine with embargo and CWA scoring"
```

---

## Task 10: Build Per-Asset Weight Optimizer (Grid Search)

**Files:**
- Create: `tools/weight_optimizer.py` — Grid search over 3-dimension weight combos
- Create: `tests/test_weight_optimizer.py`

**Step 1: Write failing tests**

```python
def test_generate_weight_grid():
    """Grid must produce ~66 valid weight combos for 3 dimensions."""
    grid = generate_weight_grid(n_dims=3, step=0.05, min_weight=0.05, max_weight=0.70)
    assert 50 <= len(grid) <= 80
    for combo in grid:
        assert abs(sum(combo) - 1.0) < 0.001
        assert all(w >= 0.05 for w in combo)
        assert all(w <= 0.70 for w in combo)

def test_optimizer_selects_best_weights():
    """Optimizer must select weight combo that maximizes CWA."""
    # Synthetic data where technical is clearly best
    results = run_optimizer(synthetic_data_technical_dominant)
    assert results["BTC"]["weights"]["technical"] > 0.40
    assert results["BTC"]["cwa"] > 0.0

def test_optimizer_ic_sub_weights():
    """Sub-weights within a dimension must be proportional to IC."""
    ic_values = {"rsi": 0.10, "macd": 0.05, "obv": 0.15, "mfi": -0.03}
    sub_weights = compute_ic_sub_weights(ic_values)
    assert sub_weights["obv"] > sub_weights["rsi"] > sub_weights["macd"]
    assert sub_weights["mfi"] == 0.0  # Negative IC → zero weight
    assert abs(sum(sub_weights.values()) - 1.0) < 0.001
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_weight_optimizer.py -v
```

**Step 3: Implement weight optimizer**

Create `tools/weight_optimizer.py`:

```python
"""Per-asset weight optimizer using grid search + IC-based sub-weights."""

import numpy as np
from itertools import product
from scipy.stats import spearmanr

def generate_weight_grid(n_dims: int = 3, step: float = 0.05,
                         min_weight: float = 0.05, max_weight: float = 0.70) -> list[tuple]:
    """Generate all valid weight combinations that sum to 1.0."""
    values = np.arange(min_weight, max_weight + step/2, step)
    values = np.round(values, 2)

    combos = []
    for combo in product(values, repeat=n_dims - 1):
        remainder = round(1.0 - sum(combo), 2)
        if min_weight <= remainder <= max_weight:
            combos.append(tuple(combo) + (remainder,))
    return combos

def compute_ic(indicator_scores: list, forward_returns: list) -> float:
    """Spearman rank correlation (IC). Returns 0.0 if not significant."""
    if len(indicator_scores) < 20:
        return 0.0
    corr, p_value = spearmanr(indicator_scores, forward_returns)
    return float(corr) if p_value < 0.05 else 0.0

def compute_ic_sub_weights(ic_dict: dict) -> dict:
    """Convert IC values to normalized sub-weights. Negative IC → 0."""
    clipped = {k: max(0, v) for k, v in ic_dict.items()}
    total = sum(clipped.values())
    if total == 0:
        n = len(clipped)
        return {k: 1.0/n for k in clipped}
    return {k: v/total for k, v in clipped.items()}

def optimize_weights_for_asset(
    asset: str,
    dimension_scores: dict,  # {day_idx: {"technical": score, "derivatives": score, "market": score}}
    forward_returns_24h: list,
    forward_returns_48h: list,
    noise_threshold: float,
    strong_threshold: float,
    atr_band_pct: float,
) -> dict:
    """Find optimal weights for one asset via grid search."""
    from tools.walk_forward import gradient_score, evaluate_neutral, compute_cwa

    grid = generate_weight_grid()
    best_score = -1
    best_weights = (0.34, 0.33, 0.33)
    dim_names = ["technical", "derivatives", "market"]

    for weights in grid:
        total_correct = 0
        total_signals = 0
        total_directional = 0

        for day_idx, dim_scores in dimension_scores.items():
            # Compute composite
            composite = sum(dim_scores[d] * w for d, w in zip(dim_names, weights))

            # Determine direction
            if composite > 55:
                direction = "bullish"
            elif composite < 45:
                direction = "bearish"
            else:
                direction = "neutral"

            actual_24h = forward_returns_24h[day_idx]
            actual_48h = forward_returns_48h[day_idx]

            if direction in ("bullish", "bearish"):
                total_directional += 1
                g24 = gradient_score(direction, actual_24h, noise_threshold, strong_threshold)
                g48 = gradient_score(direction, actual_48h, noise_threshold, strong_threshold)
                score = 0.5 * g24 + 0.5 * g48
                total_correct += score
            else:
                score = evaluate_neutral(actual_24h, atr_band_pct)
                total_correct += score

            total_signals += 1

        cwa = compute_cwa(int(total_correct), total_signals, total_directional)
        if cwa > best_score:
            best_score = cwa
            best_weights = weights

    return {
        "weights": dict(zip(dim_names, best_weights)),
        "cwa": best_score,
        "n_signals": len(dimension_scores),
    }
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/test_weight_optimizer.py -v
```

**Step 5: Commit**

```bash
git add tools/weight_optimizer.py tests/test_weight_optimizer.py
git commit -m "feat: per-asset weight optimizer with grid search and IC sub-weights"
```

---

## Task 11: Build Deploy Gate

**Files:**
- Create: `tools/deploy_gate.py` — Compare proposed vs baseline CWA
- Modify: `tools/backtest.py` — Add `--gate` and `--update-baseline` flags
- Create: `tests/test_deploy_gate.py`

**Step 1: Write failing tests**

```python
def test_deploy_gate_passes_when_cwa_improves():
    baseline = {"overall_cwa": 0.30, "assets": {"BTC": {"cwa": 0.35}}}
    proposed = {"overall_cwa": 0.32, "assets": {"BTC": {"cwa": 0.36}}}
    result = check_deploy_gate(baseline, proposed)
    assert result["passed"] is True

def test_deploy_gate_fails_when_cwa_regresses():
    baseline = {"overall_cwa": 0.30, "assets": {"BTC": {"cwa": 0.35}}}
    proposed = {"overall_cwa": 0.25, "assets": {"BTC": {"cwa": 0.30}}}
    result = check_deploy_gate(baseline, proposed)
    assert result["passed"] is False
    assert "overall_cwa" in result["failures"]

def test_deploy_gate_fails_when_asset_drops_15pct():
    baseline = {"overall_cwa": 0.30, "assets": {"BTC": {"cwa": 0.40}}}
    proposed = {"overall_cwa": 0.31, "assets": {"BTC": {"cwa": 0.30}}}  # 25% drop
    result = check_deploy_gate(baseline, proposed)
    assert result["passed"] is False
    assert "BTC" in str(result["failures"])

def test_deploy_gate_fails_high_abstain_miss():
    baseline = {"overall_cwa": 0.30, "assets": {"BTC": {"cwa": 0.35, "abstain_miss_rate": 0.20}}}
    proposed = {"overall_cwa": 0.31, "assets": {"BTC": {"cwa": 0.36, "abstain_miss_rate": 0.35}}}
    result = check_deploy_gate(baseline, proposed)
    assert result["passed"] is False
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_deploy_gate.py -v
```

**Step 3: Implement deploy gate**

Create `tools/deploy_gate.py`:

```python
"""Deploy gate — blocks changes that regress CWA."""

import json
from pathlib import Path

BASELINE_PATH = Path(__file__).parent.parent / "backtest_baseline.json"

def load_baseline() -> dict | None:
    if BASELINE_PATH.exists():
        return json.loads(BASELINE_PATH.read_text())
    return None

def save_baseline(results: dict):
    BASELINE_PATH.write_text(json.dumps(results, indent=2))

def check_deploy_gate(baseline: dict, proposed: dict,
                      max_asset_drop: float = 0.15,
                      max_abstain_miss: float = 0.30) -> dict:
    """Check if proposed results pass the deploy gate."""
    failures = []

    # Condition 1: Overall CWA must not regress
    if proposed["overall_cwa"] < baseline["overall_cwa"]:
        failures.append(f"overall_cwa: {baseline['overall_cwa']:.3f} → {proposed['overall_cwa']:.3f}")

    # Condition 2: No individual asset drops by more than max_asset_drop
    for asset, b_data in baseline.get("assets", {}).items():
        p_data = proposed.get("assets", {}).get(asset, {})
        if p_data and b_data.get("cwa", 0) > 0:
            drop = (b_data["cwa"] - p_data.get("cwa", 0)) / b_data["cwa"]
            if drop > max_asset_drop:
                failures.append(f"{asset} CWA dropped {drop:.1%} (max {max_asset_drop:.0%})")

    # Condition 3: Abstain miss rate stays controlled
    for asset, p_data in proposed.get("assets", {}).items():
        miss_rate = p_data.get("abstain_miss_rate", 0)
        if miss_rate > max_abstain_miss:
            failures.append(f"{asset} abstain_miss_rate={miss_rate:.2f} > {max_abstain_miss}")

    return {"passed": len(failures) == 0, "failures": failures}
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/test_deploy_gate.py -v
```

**Step 5: Commit**

```bash
git add tools/deploy_gate.py tests/test_deploy_gate.py
git commit -m "feat: deploy gate — blocks changes that regress CWA"
```

---

## Task 12: Wire Everything Together — Full Backtest Runner

**Files:**
- Modify: `tools/backtest.py` — Rewrite to use walk-forward, weight optimizer, deploy gate
- Test: `tests/test_backtest_integration.py`

**Step 1: Write integration test**

```python
def test_full_backtest_produces_per_asset_results():
    """Full backtest must produce per-asset CWA, weights, and IC rankings."""
    results = run_full_backtest(days=30, use_mock_data=True)  # Short for testing
    assert "overall_cwa" in results
    assert "assets" in results
    for asset in ["BTC", "ETH"]:
        assert asset in results["assets"]
        assert "weights" in results["assets"][asset]
        assert "cwa" in results["assets"][asset]
        assert sum(results["assets"][asset]["weights"].values()) - 1.0 < 0.01
```

**Step 2: Rewrite backtest.py**

Wire together:
1. `historical_fetcher.py` → fetch data
2. Compute indicators per asset per day (using technical agent logic)
3. `walk_forward.py` → generate folds
4. `weight_optimizer.py` → find best weights per fold per asset
5. `deploy_gate.py` → compare to baseline

Add CLI flags:
```bash
python3 -m tools.backtest --full          # Full walk-forward backtest
python3 -m tools.backtest --quick         # Last 7 days only
python3 -m tools.backtest --gate          # Run deploy gate comparison
python3 -m tools.backtest --update-baseline  # Save results as new baseline
```

**Step 3: Run integration test**

```bash
python3 -m pytest tests/test_backtest_integration.py -v
```

**Step 4: Run full backtest on real data**

```bash
python3 -m tools.backtest --full
```

Present results to user. Expected output format:

```
BACKTEST RESULTS (Phase 1: 180 days, Phase 2: 90 days)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Asset  Signals  Dir  Correct  Acc%   Coverage  CWA%
BTC    180      81   53       65.4%  45.0%     29.4%
ETH    180      72   43       59.7%  40.0%     23.9%
...
ALL    2160     810  486      60.0%  37.5%     22.5%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Abstain miss rate: 18.5%
```

**Step 5: Save baseline and commit**

```bash
python3 -m tools.backtest --full --update-baseline
git add tools/backtest.py backtest_baseline.json tests/test_backtest_integration.py
git commit -m "feat: full walk-forward backtest with per-asset weights and deploy gate"
```

---

## Task 13: Apply Optimized Weights to Config

**Files:**
- Modify: `scoring/pipeline.py` — Load per-asset weights from backtest_baseline.json
- Modify: `scoring/config.py` — Add per-asset weight loading
- Test: `tests/test_pipeline.py`

**Step 1: Write failing test**

```python
def test_pipeline_uses_per_asset_weights():
    """Pipeline must use per-asset weights from backtest baseline, not global."""
    # Create baseline with BTC=heavy technical, ETH=heavy market
    baseline = {
        "assets": {
            "BTC": {"weights": {"technical": 0.50, "derivatives": 0.20, "market": 0.30}},
            "ETH": {"weights": {"technical": 0.25, "derivatives": 0.15, "market": 0.60}},
        }
    }
    signals = fuse_signals(data, cfg, assets_cfg, baseline=baseline)
    # BTC and ETH should produce different composites even with same raw scores
    # because their weights differ
```

**Step 2: Implement per-asset weight loading**

In `scoring/pipeline.py`, at the start of `fuse_signals()`:

```python
# Load per-asset weights from backtest baseline if available
baseline_path = Path(__file__).parent.parent / "backtest_baseline.json"
per_asset_weights = {}
if baseline_path.exists():
    baseline = json.loads(baseline_path.read_text())
    for asset, data in baseline.get("assets", {}).items():
        if data.get("confidence") in ("high", "medium"):
            per_asset_weights[asset] = data["weights"]
```

In the weight selection step, check `per_asset_weights` first before falling back to config weights.

**Step 3: Run tests**

```bash
python3 -m pytest tests/ -v
```

**Step 4: Commit**

```bash
git add scoring/pipeline.py scoring/config.py tests/test_pipeline.py
git commit -m "feat: pipeline loads per-asset weights from backtest baseline"
```

---

## Task 14: Abstain Threshold Calibration Sweep

**Files:**
- Create: `tools/abstain_sweep.py` — Sweep abstain thresholds per asset
- Test: `tests/test_abstain_sweep.py`

**Step 1: Write failing test**

```python
def test_abstain_sweep_finds_optimal_thresholds():
    """Sweep must find thresholds that maximize CWA while controlling miss rate."""
    result = sweep_abstain_thresholds(
        signals=mock_signals,
        bearish_range=[3, 5, 8],
        bullish_range=[4, 6, 10],
        regime_mult_range=[0.8, 1.0, 1.2],
    )
    assert "best_bearish" in result
    assert "best_bullish" in result
    assert "best_regime_mult" in result
    assert result["abstain_miss_rate"] < 0.30
```

**Step 2: Implement sweep**

Create `tools/abstain_sweep.py`:

```python
def sweep_abstain_thresholds(signals, bearish_range, bullish_range, regime_mult_range):
    """Find per-asset abstain thresholds that maximize accuracy*coverage."""
    best = {"score": -1}
    for b in bearish_range:
        for bu in bullish_range:
            for rm in regime_mult_range:
                # Re-apply abstain with these thresholds
                # Compute CWA + abstain_miss_rate
                score = 0.4 * cwa + 0.3 * accuracy + 0.3 * (1 - miss_rate)
                if score > best["score"]:
                    best = {"score": score, "best_bearish": b, "best_bullish": bu,
                            "best_regime_mult": rm, "abstain_miss_rate": miss_rate}
    return best
```

**Step 3: Run tests**

```bash
python3 -m pytest tests/test_abstain_sweep.py -v
```

**Step 4: Commit**

```bash
git add tools/abstain_sweep.py tests/test_abstain_sweep.py
git commit -m "feat: per-asset abstain threshold calibration sweep"
```

---

## Task 15: Run Full Optimization & Deploy

**This is the final manual step — not automated.**

**Step 1: Run full backtest**

```bash
cd /Users/admin/Documents/web3-signals
python3 -m tools.backtest --full
```

Review output. Present per-asset results to user.

**Step 2: Run abstain sweep**

```bash
python3 -m tools.abstain_sweep
```

Review per-asset optimal thresholds.

**Step 3: Save baseline**

```bash
python3 -m tools.backtest --full --update-baseline
```

**Step 4: Run deploy gate against baseline**

```bash
python3 -m tools.backtest --full --gate
```

Verify PASS.

**Step 5: Run full test suite**

```bash
python3 -m pytest tests/ -v
```

**Step 6: Commit and push**

```bash
git add -A
git commit -m "feat: first production backtest baseline with per-asset optimized weights"
git push origin v2
```

**Step 7: Verify signals**

```bash
python3 -c "
from scoring.config import load_config
from scoring.pipeline import fuse_signals, ALL_DIMENSIONS
# Load latest agent data and run fusion
# Verify signals are no longer 91% ABSTAIN
"
```

Present final results to user for approval before Railway deploy.

---

## Summary

| Task | Description | Effort | Dependencies |
|------|-------------|--------|-------------|
| 1 | Cut dead agents | Quick | None |
| 2 | Fix OI change % | Quick | None |
| 3 | Fix market agent macro | Quick | None |
| 4 | Add OBV + MFI | Medium | Task 1 |
| 5 | Add ROC, StochRSI, squeeze, z-scores | Medium | Task 4 |
| 6 | Add stablecoin, BTC dom, VIX ROC, NASDAQ | Medium | Task 3 |
| 7 | Add OI-weighted funding + relative features | Medium | Tasks 2, 4 |
| 8 | Build historical data fetcher | Medium | None |
| 9 | Build walk-forward engine | Large | None |
| 10 | Build weight optimizer | Large | Task 9 |
| 11 | Build deploy gate | Quick | Task 10 |
| 12 | Wire full backtest runner | Large | Tasks 8-11 |
| 13 | Apply optimized weights in pipeline | Medium | Task 12 |
| 14 | Abstain threshold sweep | Medium | Task 12 |
| 15 | Run optimization & deploy | Manual | All above |

**Parallelizable:** Tasks 1-3 can run in parallel. Tasks 4-7 can run in parallel. Tasks 8-9 can run in parallel.

**Critical path:** Tasks 1 → 4 → 8 → 9 → 10 → 12 → 13 → 15
