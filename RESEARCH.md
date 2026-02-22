# x402 Protocol — Research Notes

## What is x402?
An open payment protocol that revives the HTTP 402 "Payment Required" status code to embed stablecoin micropayments natively into HTTP traffic. Launched by Coinbase on May 6, 2025.

**Core concept:** Machines paying machines. No human involved. No login. No subscription. Just use and pay instantly in USDC.

---

## Backed By
Coinbase, Cloudflare, Google, Anthropic, Visa, AWS, Circle, Vercel, NEAR

---

## Scale
- 100M+ payments processed (as of V2 launch, Dec 2025)
- 5,500+ GitHub stars
- 150+ ecosystem projects
- Solana + Base are primary chains

---

## How It Works (Simple Version)
1. Agent/user calls an API
2. Server responds: "Pay $0.10 USDC first"
3. Client signs payment automatically from wallet
4. Server receives payment confirmation
5. Server returns the data
All in one HTTP round-trip. Milliseconds.

---

## What People Are Building (Categories)

### Already Crowded (Avoid)
- Generic data APIs (CoinGecko, QuickNode)
- Developer SDKs and tooling (20+ projects)
- LLM gateways
- DeFi yield finders
- Shopify commerce integration

### Hackathon Winners So Far
- Paystabl — decentralized payroll agent
- Intelligence Cubed (i³) — AI model tokenization
- PlaiPin — ESP32 IoT chip with crypto wallet
- x402 Shopify Commerce — AI customer integration
- Superfluid x402-sf — subscription payment streams

---

## Real Gaps in the Ecosystem (Opportunities)

1. **Service Discovery** — No directory for agents to find x402 APIs
2. **Agent Budget Management** — No dashboard showing what agents are spending
3. **Vertical-Specific Apps** — Everything is generic; no niche focus
4. **Analytics Layer** — No Dune Analytics equivalent for x402
5. **USDT Support** — Only USDC works (USDT has no EIP-3009)
6. **Cross-chain Abstraction** — Must specify chain manually
7. **Dispute Resolution** — No refund/escrow production system
8. **Enterprise Compliance** — No KYC/AML/audit layer

---

## Our Opportunity
Crypto signals + narratives vertical is COMPLETELY UNTOUCHED.
Nobody is building curated, opinionated alpha as an x402 pay-per-query API.

---

## Key Links
- GitHub: https://github.com/coinbase/x402
- Ecosystem: https://www.x402.org/ecosystem
- Docs: https://www.x402.org
- x402 V2: https://www.x402.org/writing/x402-v2-launch
- Hackathons: https://dorahacks.io/hackathon/cronos-x402/buidl
