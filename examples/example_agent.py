"""
Example: pay-per-call crypto signal via x402 protocol.

Sends $0.001 USDC on Base to fetch a single-asset signal from the
Web3 Signals API.  Demonstrates the full x402 payment handshake:
  1) GET /signal/BTC  ->  HTTP 402 + payment-required header
  2) Parse & sign the payment with your wallet
  3) Retry with the signed payment header  ->  HTTP 200 + signal

Install:
    pip install x402 eth-account httpx

Run:
    python example_agent.py 0xYOUR_PRIVATE_KEY
    # or
    AGENT_WALLET_KEY=0x... python example_agent.py

Your wallet needs >= 0.001 USDC on Base (chain 8453).
Bridge at https://bridge.base.org or via Coinbase.
"""

import sys, os, base64, httpx
from eth_account import Account
from x402 import x402ClientSync, parse_payment_required
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.http.utils import encode_payment_signature_header

API = "https://web3-signals-api-production.up.railway.app"
ASSET = "BTC"


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else os.getenv("AGENT_WALLET_KEY")
    if not key:
        sys.exit("Usage: python example_agent.py <PRIVATE_KEY>\n"
                 "       or set AGENT_WALLET_KEY env var")

    account = Account.from_key(key)
    signer = EthAccountSigner(account)
    client = x402ClientSync()
    client.register("eip155:8453", ExactEvmClientScheme(signer))

    url = f"{API}/signal/{ASSET}"
    print(f"\U0001f50d Requesting {ASSET} signal from Web3 Signals API...")

    with httpx.Client(timeout=60) as http:
        # Step 1 -- initial request triggers a 402 challenge
        resp = http.get(url)
        if resp.status_code != 402:
            sys.exit(f"Expected 402, got {resp.status_code}: {resp.text[:200]}")

        # Step 2 -- parse the payment terms from the 402 response
        payment_req = parse_payment_required(
            base64.b64decode(resp.headers["payment-required"])
        )
        amt = int(payment_req.accepts[0].amount) / 1e6   # USDC has 6 decimals
        print(f"\U0001f4b0 Payment required: ${amt:.3f} USDC on Base")

        # Step 3 -- sign the payment payload
        payload = client.create_payment_payload(payment_req)
        header_name = ("payment-signature"
                       if payload.x402_version == 2 else "x-payment")
        encoded = encode_payment_signature_header(payload)

        # Step 4 -- retry with the signed payment header
        resp = http.get(url, headers={header_name: encoded})
        if resp.status_code != 200:
            sys.exit(f"Payment rejected ({resp.status_code}): {resp.text[:300]}")

    # Step 5 -- display the signal
    data = resp.json()
    sig = data["signal"]
    ctx = data.get("market_context", {})
    print("\u2705 Payment accepted! Signal received:\n")
    print(f"  Asset:     {data['asset']}")
    print(f"  Score:     {sig['composite_score']}/100")
    print(f"  Direction: {sig['direction']}")
    print(f"  Label:     {sig['label']}")
    print(f"  Regime:    {ctx.get('regime', 'N/A')}")
    print(f"  Risk:      {ctx.get('risk_level', 'N/A')}")


if __name__ == "__main__":
    main()
