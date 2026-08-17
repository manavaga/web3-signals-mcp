# Directory Submissions Pack — Web3 Signals

Ready-to-paste content for every listing target. Canonical identity — use these
exact strings everywhere so LLMs and indexers converge on one entity:

- **Name:** Web3 Signals
- **One-liner:** AI-powered crypto signal intelligence for 20 assets — whale, technical, derivatives, narrative, market and trend dimensions fused into 0-100 scores, with honest confidence-gated accuracy reporting. Payable by AI agents via x402 micropayments ($0.001 USDC on Base).
- **API base:** https://web3-signals-api-production.up.railway.app
- **MCP SSE:** https://web3-signals-api-production.up.railway.app/mcp/sse
- **MCP Streamable HTTP:** https://web3-signals-api-production.up.railway.app/mcp/stream
- **Repo:** https://github.com/manavaga/web3-signals-mcp (MIT)
- **Registry ID:** io.github.manavaga/web3-signals

## Long description (for forms with room)

Web3 Signals is a crypto signal intelligence API built for AI agents. Five data
pipelines (whale tracking, technical analysis, derivatives positioning, narrative
sentiment, market data) run continuously and fuse into a 0-100 composite score per
asset across 20 majors (BTC, ETH, SOL, ...). Free tier: prices, score bands,
direction, market regime, and a fully transparent accuracy/reputation endpoint with
a published confidence gate (scores are withheld unless backed by sufficient recent
evaluations — no inflated claims). Paid tier ($0.001 USDC per call via the x402
protocol on Base): exact scores, full 6-dimension breakdown, LLM insights, and
per-asset trading levels. MCP server with 10 tools for Claude Desktop, Cursor, and
any MCP client; agent discovery via .well-known cards, llms.txt, and OpenAPI.

## Targets

### 1. PulseMCP — https://www.pulsemcp.com (submit form in footer / "Submit a server")
Paste: name, repo URL, MCP endpoints, long description. No auth wall reported.

### 2. Smithery — https://smithery.ai (Sign in with GitHub → Add server)
Needs your GitHub login. Point at the repo; it reads server metadata. Add the
`smithery.yaml` if their flow requests one (their onboarding will say).

### 3. Glama (claim existing listing) — https://glama.ai/mcp/servers/manavaga/web3-signals-mcp
Sign in with GitHub → "Claim this server". LICENSE (now MIT) fixes the License F;
claiming + a maintainer response fixes Maintenance F. Re-request inspection after
claiming so the "cannot be installed" flag is retested.

### 4. x402list.fun — submission form on site
Submit API base URL + description. Emphasize the x402 endpoints and price.

### 5. mcp.so — https://mcp.so (submit via GitHub issue or form on site)
Same canonical content.

### 6. LobeHub — https://lobehub.com/mcp (submit server)
Same canonical content.

### 7. Anthropic — Claude connectors directory
https://www.anthropic.com/partners/mcp (partner/connector submission). Use the
long description; stress remote MCP (SSE + streamable HTTP), read-only tools,
no auth required for free tier.

### 8. OpenAI — ChatGPT Apps SDK / custom GPT
- Apps SDK (apps in ChatGPT are MCP-based): developer submission via
  https://platform.openai.com/docs/apps-sdk when app review is open — our
  remote MCP endpoint is the integration surface.
- Interim: create a custom GPT ("Web3 Signals — Crypto Signal Intelligence")
  with Actions pointing at our OpenAPI (https://web3-signals-api-production.up.railway.app/openapi.json),
  publish to the GPT Store under your builder profile.

### 9. Coinbase Bazaar (x402 discovery) — automatic
No form. Indexing is driven by successful paid settlements through the CDP
facilitator + the Bazaar discovery extension we register at boot. To refresh
after downtime: run `python3 scripts/payment_bot.py --count 2` (hits /signal
and /signal/BTC). Verify presence:
`curl "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources" | grep web3-signals`

## After each submission
Add the listing URL to the GEO scorecard config (`scripts/geo_scorecard.py`) so
weekly runs track its status.
