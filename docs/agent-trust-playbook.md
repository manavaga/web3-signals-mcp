# Making APIs Trustable to AI Agents — A Playbook

*How autonomous agents discover, evaluate, trust, and pay for a machine-priced API — and the specific practices that earn each step. Learnings from operating Web3 Signals x402 (an AI crypto-signals API monetized via x402 micropayments on Base), grounded in ~480k real requests and a live payment funnel.*

---

## The core thesis

For an AI agent, **discovery ≠ comprehension ≠ trust ≠ payment ≠ retention.** Each is a separate gate, and traffic decays at every one. Our biggest mistake was optimizing the first gate (discoverability) and assuming the rest followed. They don't.

The numbers that made this concrete:

- **~480k lifetime requests, ~101k "external", 1,637 unique external clients** — strong discovery.
- **16,071 payment challenges → 20 genuine external paid calls** — a real conversion of **~0.12%**.
- Of the 177 "paid" calls our dashboard celebrated, **157 were our own test scripts**. Real external revenue was **$0.02**.
- Every top consuming agent (opencode, claude-code, MCP clients, indexers) had **0 payment challenges and 0 paid calls** — they lived entirely on free/discovery surfaces and never reached the wall.

The lesson: **being listed everywhere is supply-side vanity. Agents convert on evidence and incentive, not on presence.**

---

## The agent trust funnel

```
Discoverability → Comprehension → Trust/Verification → Payment → Integration → Retention
```

Each section below: the principle, what we got wrong, and the practice that earns the gate.

---

### 1. Discoverability — necessary, not sufficient

**Principle:** An agent can only use what it can find and parse without a human. Standard, machine-readable discovery surfaces (MCP, `/.well-known/*`, OpenAPI, agent cards) are table stakes.

**What we observed:** Discovery worked *too* well — MCP transports and `.well-known/x402` were crawled tens of thousands of times by directories, scorers, and uptime monitors. But that traffic was **monitoring, not demand.** Discoverability is the gate you'll over-invest in because it produces the most flattering graphs.

**Practices that earn it:**
- Publish the standard cards: `/.well-known/x402`, `agent.json`/agent-card, MCP `server-card`, OpenAPI. Keep them **consistent with each other** (same endpoints, prices, schemas).
- **Make discovery deterministic.** We logged ~7,000 `4xx` from agents probing nonexistent variants (`/swagger.json`, `/v1/openapi.yaml`, `/.well-known/openapi.json`, …). Agents *guess* when the path isn't where they expect. Serve the common variants or redirect them; don't make agents brute-force your metadata.
- **Don't confuse crawler volume with interest.** Segment directory/health-check/scoring bots out of your "traffic" before you reason about demand (see §7).

---

### 2. Comprehension — the agent must model what it gets *before* it pays

**Principle:** An agent decides whether to pay based on a schema and a sample, not marketing copy. If it can't predict the shape and value of the paid response, it won't spend.

**Practices that earn it:**
- **Rich input/output schemas in the discovery card** (we use Bazaar discovery extensions): every field typed and described, enums enumerated, units stated.
- **A live sample in the 402 body**, not a static example. Our `402` returns a real (truncated) signal preview with `_dimensions in paid response` markers — the agent sees freshness and exactly what unlocks on payment.
- **Document the unit of value precisely.** What is one paid call? What's cached vs. fresh? What's the refresh cadence? Agents plan around this.

---

### 3. Trust / Verification — the single most underrated gate

**Principle:** An agent paying for *predictions* needs **verifiable, current evidence of quality** before spending. A claimed accuracy number it cannot audit is worth nothing — and actively erodes trust if it turns out to be stale or self-serving.

**What we got badly wrong (the cautionary tale):**
- We sold a `/performance/reputation` "accuracy score" — and it had quietly become a **coin flip (50%) computed on 15 stale samples**, with the last real evaluation **~3 months old** and our Information Coefficient `null`.
- The score *looked* authoritative (`status: active`, a number, a methodology block) while being statistically meaningless. The methodology text even described a mechanism that was no longer the active code path. **This is the fastest way to lose an agent's trust permanently:** an auditing agent (and agents *do* audit) finds the claim is unsupported.

**Practices that earn it:**
- **Gate every published quality metric behind a confidence test.** We now withhold the reputation score unless it rests on **≥100 directional evaluations, <72h freshness, and ≥10% coverage**. Below that, the endpoint returns `status: insufficient_data`, `reputation_score: null`, and explicit `data_quality_caveats` — the raw numbers stay visible "for transparency only," but we never present a coin flip as a track record.
- **Expose freshness and sample size as first-class fields** (`last_evaluated_at`, `directional_signals_evaluated`, `directional_coverage`, `hours_since_last_evaluation`). Let the agent apply its own threshold.
- **Make the trust artifact free.** Verification data must come *before* the payment, not behind it. A reputation/track-record endpoint that itself costs money is a contradiction — the agent needs it to decide whether to pay at all.
- **State methodology honestly and keep it synced to the code.** If you abstain on most signals, say so and report the coverage; don't let an impressive accuracy number hide that it's computed on a 2% sliver.
- **Beware metrics that are gamed by abstention.** We optimized "accuracy" so hard that the engine abstained on ~98% of signals — accuracy looked great because we'd stopped making falsifiable calls. A trustable quality metric must report **coverage alongside accuracy**; high accuracy at near-zero coverage is not a product.

---

### 4. Payment — put the wall where the agents actually are

**Principle:** The payment surface must intersect the surface the agent already uses. If value is reachable for free on one surface and the paywall sits on another the agent never traverses, you will never convert — regardless of price.

**What we got wrong:** We had **two disconnected surfaces** — a free MCP surface where all the agents lived, and a paid REST surface where only our own test scripts lived. The data proved it: the heavy MCP/coding agents had **0 payment challenges** — they never even reached the wall, because they got what they needed for free over MCP. This is an **incentive/placement problem, not a UX or pricing problem.**

**Practices that earn it:**
- **Gate the surface agents use.** If agents arrive via MCP `tools/call`, return the `402`-equivalent *in-band* in the MCP response (teaser as the free tier), rather than expecting them to discover a separate paid REST route.
- **Emit payment metadata where the agent is already looking** (x402 details in the MCP tool schema / 402 header) so a wallet-capable agent can auto-sign without context-switching out of its tool loop.
- **Price isn't the objection at $0.001.** Our ~0.12% conversion wasn't a pricing failure — it was that no agent had a *reason* (trust) or an *intersecting wall* (placement) to pay. Fix trust and placement before touching price.
- **Keep the payment mechanics healthy** — this part *did* work for us: 178 attempts, 1 failure. A reliable x402/facilitator path is necessary but, as the funnel shows, far from sufficient.

---

### 5. Integration — the first paid call must be one frictionless round-trip

**Principle:** The cost of integration is paid in agent-developer (and agent) effort. Every extra step, SDK, or surface-switch is attrition.

**The frictionless ideal for an LLM agent:**
> One tool call → server returns `402` in-band with teaser + price + accepts-array → the agent's wallet middleware auto-signs USDC → it retries the same call with the payment header → it gets the data. **One tool, one round-trip, no surface-switch, no separate SDK context.**

**Practices that earn it:**
- Support the agent-native payment path (x402-fetch / wallet middleware / CDP AgentKit-style clients) end-to-end, not just a Python SDK example.
- Make retry-after-payment idempotent and obvious from the `402` headers.
- Keep paid latency low (see §6) — a `402` round-trip plus signing plus retry already adds overhead; don't compound it.

---

### 6. Performance & reliability — latency is part of trust for machines

**Principle:** Agents make programmatic, sometimes high-frequency decisions. Slow or flaky responses get an integration dropped silently.

**What we observed / are fixing:**
- **Paid latency ~1.2s** for a $0.001 call is too high for volume machine-to-machine use. Serve paid reads from a **precomputed cache** (the fusion already runs on a cadence — reads should be O(1) lookups, not recomputation). Target sub-200ms.
- A background data agent was **fetching 7 sources serially** and tripping the 120s orchestrator ceiling, intermittently degrading freshness. We parallelized it (per-source timeout; total ≈ slowest source, not the sum). **Reliability of the data behind the signal is part of the product an agent is paying for.**
- Return correct, stable cache headers so well-behaved agents can cache and not hammer you.

---

### 7. Measurement integrity — you can't earn trust you can't measure

**Principle:** If your own metrics are polluted or stale, you will optimize the wrong gate and mislead both yourself and your customers.

**What we got wrong:**
- **Self-traffic pollution:** 157 of 177 "paid" calls were our own tests; the dashboard headline didn't separate internal from external, inflating apparent traction ~9×. We now split paid calls **external vs internal** and headline only genuine external payments (self-tests annotated).
- **Inconsistent definitions:** "AI agent" was counted differently across endpoints; 17–24% of traffic was `unclassified` — larger than the "external" bucket itself. You can't trust an internal-vs-external split when a quarter of traffic is unlabeled.
- **Stale measurement chains:** snapshots accumulated while evaluations silently stopped, so dashboards showed "fresh" data on top of a dead pipeline. **A null/insufficient metric should be a loud failure, not a quiet field.**

**Practices that earn it:**
- Exclude internal/self fingerprints from every headline KPI by default.
- One canonical definition for each segment (agent, bot, external), reused everywhere.
- Drive `unclassified` traffic down; you can't fix a funnel you can't see.
- Make freshness and sample-size visible on every quality metric, and alarm on staleness.

---

## Anti-patterns checklist (what to *stop* doing)

- ❌ **Selling a quality score you've stopped measuring.** Stale/insufficient → withhold the number, return `insufficient_data`.
- ❌ **Faking accuracy by abstaining.** Report coverage next to accuracy; near-zero coverage = no product.
- ❌ **Putting the paywall on a surface agents don't use.** Gate the surface they're actually on.
- ❌ **Hiding the trust artifact behind the paywall.** Verification must be free and precede payment.
- ❌ **Counting crawler/health-check/self traffic as demand.** Segment it out before reasoning.
- ❌ **Non-deterministic discovery metadata.** Serve the standard paths; don't make agents guess.
- ❌ **Marketing-grade methodology text that drifts from the code.** Agents read the mechanism, not the adjectives.
- ❌ **High paid latency / recompute-on-read.** Cache it; machines notice.

---

## A trustability scorecard for any agent-facing API

Ask, for each gate:

1. **Discoverable:** Can an agent find and parse me via standard cards with zero human help, with no `4xx` guessing?
2. **Comprehensible:** Can it predict the paid response's shape and value from my schema + live sample?
3. **Verifiable:** Can it confirm my quality claim — freshness, sample size, coverage — *for free, before paying*?
4. **Reachable:** Does my paywall sit on the surface the agent already uses?
5. **Frictionless:** Is the first paid call one in-band, auto-signable round-trip?
6. **Fast & reliable:** Sub-200ms paid reads, stable, correctly cached?
7. **Honestly measured:** Are my own metrics free of self-traffic and stale data, so I'm optimizing the real gate?

If any answer is "no," that's where your conversion is leaking — and for us, it was #3 and #4, not #1.

---

*This playbook is descriptive of our own mistakes and fixes, not a guarantee. The honest summary: we nailed discoverability and over-trusted our own dashboards; agents quietly told us — with their wallets — that they need verifiable evidence and an intersecting paywall before they spend. Earn the trust gate first.*
