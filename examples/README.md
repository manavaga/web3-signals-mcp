# Web3 Signals x402 -- Example Agent

A minimal Python script that pays $0.001 USDC on Base to fetch a crypto
trading signal, demonstrating the **x402** machine-to-machine payment protocol.

## What is x402?

x402 brings the HTTP `402 Payment Required` status code to life.  When an
agent requests a paid endpoint the server responds with a 402 and a
`payment-required` header describing the price, network, and token.  The
agent signs a payment payload with its wallet and retries the request -- no
API keys, no subscriptions, just pay-per-call.

## Prerequisites

| Requirement | Details |
|-------------|---------|
| Python | 3.10+ |
| USDC on Base | >= 0.001 USDC (about one-tenth of a cent) |
| Private key | An EVM wallet key that controls the USDC |

### Getting USDC on Base

1. **Coinbase users** -- send USDC to your wallet on the Base network directly
   from the Coinbase app (zero fee).
2. **Bridge from Ethereum** -- use [bridge.base.org](https://bridge.base.org)
   to move USDC from Ethereum L1 to Base.
3. **Faucets / swaps** -- swap any Base token for USDC on Uniswap or Aerodrome.

Even $1 of USDC is enough for 1,000 API calls at $0.001 each.

## Installation

```bash
pip install x402 eth-account httpx
```

## Usage

Pass your private key as a CLI argument:

```bash
python example_agent.py 0xYOUR_PRIVATE_KEY
```

Or set it as an environment variable:

```bash
export AGENT_WALLET_KEY=0xYOUR_PRIVATE_KEY
python example_agent.py
```

### Expected output

```
Requesting BTC signal from Web3 Signals API...
Payment required: $0.001 USDC on Base
Payment accepted! Signal received:

  Asset:     BTC
  Score:     72/100
  Direction: bullish
  Label:     MODERATE BUY
  Regime:    TRENDING
  Risk:      moderate
```

## How the x402 flow works

```
Agent                          API Server                    Facilitator
  |                               |                              |
  |--- GET /signal/BTC ---------->|                              |
  |<-- 402 + payment-required ----|                              |
  |                               |                              |
  |  (parse header, sign payment) |                              |
  |                               |                              |
  |--- GET /signal/BTC ---------->|                              |
  |    + payment-signature header |--- verify + settle --------->|
  |                               |<-- payment confirmed --------|
  |<-- 200 + signal data ---------|                              |
```

1. **Request** -- `GET /signal/BTC` without any auth.
2. **Challenge** -- server returns `402` with a base64-encoded
   `payment-required` header listing accepted payment methods.
3. **Sign** -- the `x402` SDK parses the header, selects the matching scheme,
   and creates a signed payment payload using your wallet.
4. **Retry** -- the request is repeated with a `payment-signature` (v2) or
   `x-payment` (v1) header containing the signed payload.
5. **Settle** -- the server forwards the payment to a facilitator contract on
   Base which verifies the signature and transfers USDC.
6. **Response** -- the signal data is returned as JSON.

## Available endpoints

| Endpoint | Price | Description |
|----------|-------|-------------|
| `GET /signal` | $0.001 | Full 20-asset signal fusion with portfolio summary |
| `GET /signal/{asset}` | $0.001 | Single asset signal (BTC, ETH, SOL, ...) |
| `GET /performance/reputation` | $0.001 | 30-day rolling accuracy score |
| `GET /health` | free | Agent status and uptime |
| `GET /performance` | free | Overall accuracy overview |
| `GET /analytics` | free | API usage analytics |

## Full API documentation

- **OpenAPI docs** -- https://web3-signals-api-production.up.railway.app/docs
- **Agent card** -- https://web3-signals-api-production.up.railway.app/.well-known/agent.json
- **x402 info** -- https://web3-signals-api-production.up.railway.app/.well-known/x402.json
