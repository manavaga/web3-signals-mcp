# Web3 Signals Agent

## Identity
- **Name**: Web3 Signals Agent
- **Description**: AI-powered crypto signal intelligence for 20 assets. Fuses whale tracking, derivatives positioning, technical analysis, narrative momentum, and market data into scored signals with LLM insights.
- **Version**: 0.1.0
- **Provider**: Web3 Signals

## Capabilities
- Provides composite buy/sell/neutral signals for 20 crypto assets
- Portfolio-level risk assessment and market regime detection
- LLM-generated cross-dimensional insights
- Signal accuracy tracking with rolling 30-day reputation score
- Historical signal data with full audit trail

## Protocols
- **REST API**: OpenAPI-documented endpoints at /docs
- **MCP**: Model Context Protocol server (SSE transport at /mcp/sse)
- **A2A**: Agent-to-Agent discovery card at /.well-known/agent.json
- **x402**: HTTP 402 micropayments on Base mainnet (USDC) — payment is auth, no API keys

## Endpoints
| Endpoint | Method | Description | Auth |
|----------|--------|-------------|------|
| /signal | GET | All 20 asset signals with portfolio summary | x402 $0.001 |
| /signal/{asset} | GET | Single asset signal (e.g. /signal/BTC) | x402 $0.001 |
| /performance/reputation | GET | 30-day rolling accuracy score | x402 $0.001 |
| /performance/{asset} | GET | Per-asset accuracy breakdown | Free |
| /health | GET | Agent status and uptime | Free |
| /analytics | GET | API usage analytics | Free |
| /api/history | GET | Historical signal runs (paginated) | Free |

## Assets Covered
BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, MATIC, LINK, UNI, ATOM, LTC, FIL, NEAR, APT, ARB, OP, INJ, SUI

## Data Sources
1. Whale tracking (on-chain flows + exchange movements)
2. Technical analysis (RSI, MACD, MA via Binance)
3. Derivatives positioning (funding rate, OI, long/short ratio)
4. Narrative momentum (Reddit, News, CoinGecko trending)
5. Market data (price, volume, Fear & Greed Index)

## Update Frequency
- Signals refresh every 15 minutes
- LLM sentiment analysis every 12 hours
- Performance evaluation every 4 hours

## Pricing
- **$0.001/call** USDC on Base mainnet via x402 protocol
- Payment IS authentication — no API keys, no signup
- Free endpoints: /health, /dashboard, /analytics, /docs
- Facilitator: https://x402.org/facilitator

## Contact
- API Docs: /docs
- Dashboard: /dashboard
