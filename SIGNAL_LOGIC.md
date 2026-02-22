# Signal Logic & Data Sources

## Signal Definition

A STRONG BULLISH signal fires when ALL 4 conditions are met.
A MODERATE signal fires when 3 out of 4 conditions are met.
No signal otherwise.

---

## The 4 Conditions

### 1. Whale Condition
Credible whale wallet is accumulating (buying).

Credibility = ALL THREE of:
- Wallet size: holds > $1M USD equivalent
- Label: known entity on Arkham (e.g. Jump, a16z, known trader) OR Whale Alert flagged
- Track record: Arkham Smart Money score > 7.0 / 10

Action required: net accumulation in last 6 hours.

### 2. Derivatives Condition
Long/Short ratio in healthy zone — not overcrowded.

Rule: Long/Short ratio between 0.55 and 0.65
- Below 0.55 = too bearish (potential short squeeze but no conviction)
- 0.55–0.65 = healthy bullish lean ✅
- Above 0.65 = overcrowded longs, squeeze risk ❌

Supporting signal: Funding rate positive but not extreme (0% to 0.05%)

### 3. Narrative Condition
Twitter + Reddit narrative picking up but NOT at peak.

Normalised narrative score between 0.40 and 0.70 of recent 30-day peak.
- Below 0.40 = too early, no momentum
- 0.40–0.70 = early pickup, still room to run ✅
- Above 0.70 = already crowded, likely priced in ❌

### 4. Technical Condition
Trend bullish on macro + momentum timeframes.

- 30D trend: bullish (price above 30D MA, RSI > 50)
- 7D trend: bullish (price above 7D MA, MACD positive)
- 1D: neutral or bullish (not required to be bullish)

---

## Signal Output (JSON)

```json
{
  "asset": "SOL",
  "signal": "STRONG_BULLISH",
  "confidence": 0.82,
  "conditions_met": 4,
  "whale": {
    "wallet": "labelled_jump_trading",
    "action": "accumulated_2M_USDC",
    "credibility_score": 8.5,
    "hours_ago": 3
  },
  "derivatives": {
    "long_short_ratio": 0.61,
    "funding_rate": 0.02,
    "status": "healthy_not_crowded"
  },
  "narrative": {
    "score": 0.58,
    "status": "early_pickup",
    "topic": "Solana ETF speculation",
    "twitter_mentions_24h": 4200,
    "reddit_mentions_24h": 310
  },
  "technicals": {
    "30d": "bullish",
    "7d": "bullish",
    "1d": "neutral",
    "rsi_14": 58.3,
    "price_vs_30d_ma": "+4.2%"
  },
  "suggested_timeframe": "24-72hrs",
  "timestamp": "2026-02-21T10:00:00Z"
}
```

---

## Data Sources (All Free)

### Whale Layer
| Source | API | What we get |
|--------|-----|------------|
| Arkham Intelligence | https://api.arkham.intel (free tier) | Wallet labels, Smart Money score, entity names |
| Whale Alert | https://api.whale-alert.io (free tier) | Large transfer alerts, wallet addresses |
| Lookonchain | Telegram alerts (parsed) | Manual whale move alerts for context |

### Derivatives Layer
| Source | API | What we get |
|--------|-----|------------|
| Coinglass | https://open-api.coinglass.com (free) | Long/short ratio, liquidations, OI |
| Binance | https://fapi.binance.com (free) | Funding rates, futures data |
| Bybit | https://api.bybit.com (free) | Backup funding rate source |

### Narrative Layer
| Source | API | What we get |
|--------|-----|------------|
| Twitter scraper | Existing scraper | Tweet volume + sentiment per asset |
| Reddit PRAW | https://www.reddit.com/dev/api (free) | Subreddit mention counts + sentiment |

### Technical Layer
| Source | API | What we get |
|--------|-----|------------|
| CoinGecko | https://api.coingecko.com/api/v3 (free) | Price, market cap, 30D OHLCV |
| Binance | https://api.binance.com (free) | OHLCV candles for TA |
| pandas-ta | Python library (free) | RSI, MACD, MA calculations |

---

## Top 20 Assets Covered
BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, MATIC, LINK,
UNI, ATOM, LTC, FIL, NEAR, APT, ARB, OP, INJ, SUI

---

## Pricing for x402 API
- Single asset signal: $0.10 USDC
- All top-20 signals: $0.50 USDC
- Historical signal (7 days): $0.25 USDC
