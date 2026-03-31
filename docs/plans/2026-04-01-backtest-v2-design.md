# Backtest v2 + Dashboard Target Display — Design

## Problem
We've made extensive changes to agent scorers (wider ranges, MACD normalization, continuous interpolation), fusion engine (contrarian override, asymmetric weights, blacklisting), and target calculator (ATR-based SL, heuristic targets). None of these changes have been validated against historical data. The current backtest (7 parts) doesn't evaluate target accuracy, signal flow over time, prediction quality, input data health, or regime-dependent performance.

## Goals
1. Validate all recent changes against historical data with train/test separation
2. Evaluate target price accuracy (win/loss/expire rates, expected value)
3. Analyze signal flow over time (buy/sell/neutral counts per 12h bucket)
4. Audit input data quality after agent scorer changes
5. Display entry/target/SL in the dashboard

## Data Integrity — Train/Test Split
- Temporal split: first 60% of aligned time points = training, last 40% = test
- All accuracy metrics (Parts 2-12) reported from TEST window only
- Part 1 (distribution) uses full dataset for visibility
- Target calculator ML training (if invoked) restricted to training window
- Clearly labeled in output which window each metric uses

## New Backtest Parts

### PART 8: Signal Flow Timeline
Per 12h bucket (AM/PM per day):
- Count of buy / sell / neutral signals
- Which assets got directional calls (listed)
- Average composite score for the bucket
- Running accuracy (how correct were signals in this bucket?)

### PART 9: Target Price Evaluation
For each directional signal with price data:
- Walk the 48h price path to determine outcome:
  - TP_HIT: price reached target before stop loss within 48h
  - SL_HIT: price hit stop loss before target within 48h
  - EXPIRED: neither hit within 48h, record final P&L
- Aggregate metrics:
  - Win rate, loss rate, expire rate
  - Average win size vs average loss size
  - Expected value per trade: (win_rate * avg_win) - (loss_rate * avg_loss)
  - Achieved R:R vs predicted R:R
- Per-asset breakdown of target accuracy
- ATR multiplier analysis: SL hit rate by asset (too tight = hit too often)

### PART 10: Prediction Quality
- Predicted move % vs actual move % comparison
- Mean Absolute Error (MAE)
- Directional accuracy of predictions
- Calibration buckets: predicted 1-3%, 3-5%, 5%+ vs actual outcomes

### PART 11: Input Data Quality Audit
- Per-dimension score statistics: mean, std, min, max, range
- Data completeness: % of signals where each agent had real data vs "no data"
- Score clustering check: what % of scores fall within 45-55?
- Cross-dimension independence (are dimensions producing redundant info?)

### PART 12: Regime Analysis
- Accuracy split by detected regime (trending/ranging/unknown)
- Accuracy split by F&G bucket (extreme_fear/fear/neutral/greed/extreme_greed)
- Contrarian override impact: accuracy of signals WITH vs WITHOUT override

## Dashboard Changes
Add to modal detail view in `api/dashboard.py`:
- Trading Levels card: Entry Price, Target Price, Stop Loss
- Visual: green line for target, red line for SL, current price indicator
- Risk/Reward ratio badge, Confidence level, Predicted move %

## Implementation Order
1. Backtest v2 (Parts 8-12 + train/test split) — backtest.py
2. Dashboard target display — api/dashboard.py
3. Run backtest, analyze output
4. Spin agents for critical analysis
5. Autoresearch if needed
