"""
x402 Payment Bot — Bootstrap 100 real payments to force CDP Bazaar indexing.

The CDP Bazaar discovery layer ONLY indexes x402 APIs after it sees real payment
settlements flowing through the facilitator. This script makes genuine x402 payments
from an external wallet to our API, triggering the indexing pipeline.

Usage:
    python3 scripts/payment_bot.py                           # 100 payments (default)
    python3 scripts/payment_bot.py --count 50                # 50 payments
    python3 scripts/payment_bot.py --delay 2.0               # 2s between payments
    python3 scripts/payment_bot.py --key 0xPRIVATE_KEY       # custom wallet

Each payment costs $0.001 USDC on Base. 100 payments = $0.10 total.
"""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone

import httpx
from eth_account import Account
from x402 import x402ClientSync, parse_payment_required
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.http.utils import encode_payment_signature_header

API_BASE = "https://web3-signals-api-production.up.railway.app"

# All paid endpoints × valid assets for diverse traffic
ENDPOINTS = [
    # /signal — full market overview
    f"{API_BASE}/signal",
    # /signal/<asset> — per-asset signals (all 20 supported assets)
    f"{API_BASE}/signal/BTC",
    f"{API_BASE}/signal/ETH",
    f"{API_BASE}/signal/SOL",
    f"{API_BASE}/signal/BNB",
    f"{API_BASE}/signal/XRP",
    f"{API_BASE}/signal/ADA",
    f"{API_BASE}/signal/AVAX",
    f"{API_BASE}/signal/LINK",
    f"{API_BASE}/signal/DOT",
    f"{API_BASE}/signal/MATIC",
    # /performance/reputation — accuracy & track record
    f"{API_BASE}/performance/reputation",
]

# Default test wallet (different from pay-to wallet 0xdf0C4a88...)
DEFAULT_KEY = "***REMOVED***"


def make_paid_call(http: httpx.Client, x402_client, url: str) -> dict:
    """Make a single x402 paid call. Returns result dict."""
    start = time.time()
    result = {
        "url": url,
        "endpoint": url.replace(API_BASE, ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "success": False,
        "status_code": None,
        "elapsed_ms": 0,
        "payment_amount": None,
        "error": None,
    }

    try:
        # Step 1: GET → 402 challenge
        resp = http.get(url)
        if resp.status_code != 402:
            result["status_code"] = resp.status_code
            result["error"] = f"Expected 402, got {resp.status_code}"
            result["elapsed_ms"] = int((time.time() - start) * 1000)
            return result

        # Step 2: Parse payment terms
        payment_header = resp.headers.get("payment-required", "")
        if not payment_header:
            result["error"] = "No payment-required header in 402 response"
            result["elapsed_ms"] = int((time.time() - start) * 1000)
            return result

        payment_req = parse_payment_required(base64.b64decode(payment_header))
        amt = int(payment_req.accepts[0].amount) / 1e6  # USDC 6 decimals
        result["payment_amount"] = f"${amt:.4f}"

        # Step 3: Sign payment
        payload = x402_client.create_payment_payload(payment_req)
        header_name = "payment-signature" if payload.x402_version == 2 else "x-payment"
        encoded = encode_payment_signature_header(payload)

        # Step 4: Retry with payment
        resp2 = http.get(url, headers={header_name: encoded})
        result["status_code"] = resp2.status_code
        result["success"] = resp2.status_code == 200
        result["elapsed_ms"] = int((time.time() - start) * 1000)

        if resp2.status_code != 200:
            result["error"] = resp2.text[:200]

    except Exception as e:
        result["elapsed_ms"] = int((time.time() - start) * 1000)
        result["error"] = str(e)[:200]

    return result


def main():
    parser = argparse.ArgumentParser(description="x402 Payment Bot — Bootstrap Bazaar indexing")
    parser.add_argument("--key", default=DEFAULT_KEY, help="Wallet private key")
    parser.add_argument("--count", type=int, default=100, help="Number of payments (default: 100)")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between payments (default: 1.0)")
    parser.add_argument("--log", default="scripts/payment_log.jsonl", help="Log file path")
    args = parser.parse_args()

    # Setup wallet
    key = args.key if args.key.startswith("0x") else f"0x{args.key}"
    account = Account.from_key(key)
    signer = EthAccountSigner(account)
    x402_client = x402ClientSync()
    x402_client.register("eip155:8453", ExactEvmClientScheme(signer))

    cost = args.count * 0.001
    print(f"{'='*70}")
    print(f"  x402 Payment Bot — Bazaar Indexing Bootstrap")
    print(f"{'='*70}")
    print(f"  Wallet:     {account.address}")
    print(f"  Pay-to:     0xdf0C4a88CF28E24Cd63a0d2aC54052c65F0C7700")
    print(f"  Payments:   {args.count}")
    print(f"  Cost:       ${cost:.3f} USDC on Base")
    print(f"  Delay:      {args.delay}s between calls")
    print(f"  Endpoints:  {len(ENDPOINTS)} (rotating)")
    print(f"  Log:        {args.log}")
    print(f"{'='*70}\n")

    success = 0
    fail = 0
    total_ms = 0
    results = []

    with httpx.Client(timeout=120) as http:
        for i in range(args.count):
            url = ENDPOINTS[i % len(ENDPOINTS)]
            result = make_paid_call(http, x402_client, url)
            results.append(result)

            if result["success"]:
                success += 1
                total_ms += result["elapsed_ms"]
                marker = "OK"
            else:
                fail += 1
                marker = f"FAIL"

            # Progress
            pct = (i + 1) / args.count * 100
            avg_ms = total_ms // max(success, 1)
            status = f"[{i+1:3d}/{args.count}] {pct:5.1f}%"
            print(
                f"  {status}  {marker:6s}  {result['elapsed_ms']:5d}ms  "
                f"{result['endpoint']:30s}  "
                f"ok={success} fail={fail} avg={avg_ms}ms"
            )

            if result["error"]:
                print(f"           error: {result['error'][:80]}")

            # Append to log file
            with open(args.log, "a") as f:
                f.write(json.dumps(result) + "\n")

            # Delay between payments
            if i < args.count - 1:
                delay = args.delay if result["success"] else args.delay * 3
                time.sleep(delay)

    # Summary
    print(f"\n{'='*70}")
    print(f"  COMPLETE")
    print(f"{'='*70}")
    print(f"  Successful: {success}/{args.count}")
    print(f"  Failed:     {fail}/{args.count}")
    print(f"  Cost:       ${success * 0.001:.3f} USDC")
    if success > 0:
        print(f"  Avg latency: {total_ms // success}ms")
    print(f"  Log file:   {args.log}")
    print(f"{'='*70}")

    # Check analytics after
    print(f"\n  Checking analytics...")
    try:
        resp = httpx.get(f"{API_BASE}/analytics/x402", timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            print(f"  x402 analytics: {json.dumps(data, indent=2)}")
    except Exception as e:
        print(f"  Could not fetch analytics: {e}")

    return 0 if success > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
