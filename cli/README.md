# agentmarketsignal

Pipe-friendly CLI for the [Web3 Signals x402](https://web3-signals-api-production.up.railway.app) crypto market signals API.

Designed for **Layer 2 agent readiness**: every command outputs clean JSON to stdout so AI agents and scripts can consume it via pipes.

## Install

```bash
pip install ./cli            # from the repo root
# or
pip install -e ./cli         # editable / dev mode
```

## Usage

```bash
# Health check
agentmarketsignal health

# Reputation / accuracy data (free)
agentmarketsignal reputation

# Usage analytics (free)
agentmarketsignal analytics

# All 20 asset signals (x402 paid — $0.001 USDC)
agentmarketsignal signals

# Single asset signal (x402 paid)
agentmarketsignal signal BTC

# Human-readable table output
agentmarketsignal --format table reputation

# Custom API URL
agentmarketsignal --api-url http://localhost:8000 health
```

## Pipe examples

```bash
# Feed signal data into jq
agentmarketsignal reputation | jq '.accuracy_30d'

# Use in a shell script
if agentmarketsignal health > /dev/null 2>&1; then
    echo "API is up"
fi
```

## Environment variables

| Variable                        | Description                  |
|---------------------------------|------------------------------|
| `AGENTMARKETSIGNAL_API_URL`     | Override the default API URL |
| `AGENTMARKETSIGNAL_PRIVATE_KEY` | Wallet key for x402 (future) |

## x402 payment

Paid endpoints (`signals`, `signal`) require x402 micropayments ($0.001 USDC per call). Client-side payment integration is planned for a future release. The `--private-key` option is reserved for this purpose.
