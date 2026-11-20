# Owed: the agent that gets your money back

Every other shopping agent helps you buy. **Owed works after you buy.** It reads your receipts, finds money you are owed (price drops, duplicate orders, double charges), checks the store's own policy, files the claim, verifies the refund, and shows you the receipt for every decision.

Nobody enjoys fighting for refunds. Stores count on that. Owed doesn't give up, and it never acts without a policy sentence it can cite.

**Try it live: https://owed-3tov.onrender.com** and press *Run agent* (free hosting: the first load after a quiet spell can take ~50 s).

**Demo result:** 12 emails → 6 orders → 5 possible refunds → **$263.50 recovered** across 3 stores. Two cases were declined with a cited reason: a 22-day-old purchase outside a 14-day window, and a Marketplace-seller item. No human clicks.

## Agent charter

| | |
|---|---|
| **Wakes on** | New receipt email, or a daily price check |
| **Reads** | Gmail receipts (Airbyte), store policies (Senso), live store prices |
| **Can do** | File price-adjustment, return and billing-error claims at the store; email you a summary |
| **Never does** | Spend money, buy anything, share data beyond the claim, act above the autonomy limit, or act without a verified policy citation |
| **Autonomy** | Fully autonomous up to $150 per claim. Above that, or when the Statsig gate is on, it waits for your OK. |

## How it works

```
Gmail (Airbyte) ─► extract (rules + Superlinked SIE GLiNER) ─► detect (live prices, duplicates, double charges)
   ─► retrieve policy (Senso) ─► rerank passages (Superlinked SIE) ─► recall past claims with the store (Cognee)
   ─► read rule (Arcee, small model) ─► decide in code (dates, windows, exclusions) ─► autonomy gate (Statsig)
   ─► write claim (Arcee, large model) ─► fact-check the agent's reading (Senso) ─► file claim (store API)
   ─► verify refund ─► remember the outcome (Cognee) ─► notify you (Gmail)
                       every step ─► audit trail (SingleStore) + Amplitude
```

**Design choices that matter**

- **The LLM reads, the code decides.** Arcee turns a policy passage into rule parameters. Python does the date math and checks exclusions. A model never decides eligibility on its own.
- **Two independent readings of every rule.** The model's reading of the retrieved policy is cross-checked against the merchant policy registry. If they disagree, the agent uses the stricter one.
- **Verified citations.** The quoted policy sentence must appear verbatim in a retrieved Senso passage. Otherwise the case is not eligible.
- **Fact-check before acting.** The claim the agent wrote is checked claim by claim against the Senso knowledge base (`/org/evals/text`). Any conflict stops the action and hands the case to you.
- **Outcome verification.** A case counts as done only when the store confirms the refund (refund id). Filing a claim is not enough.
- **Pareto routing.** Small task-specific models (SIE) handle extraction and reranking. A small, cheap LLM reads rules. The large LLM only writes the claim. Everything else is plain code.
- **Memory is context, not authority.** Cognee remembers every outcome per store and the agent shows that history next to each decision. It never changes eligibility.
- **Every integration degrades gracefully.** If a sponsor service is down or a key is missing, that step falls back (local policy search, registry rule, message template, SQLite) and the audit trail says which path was used.

## Sponsor tools

| Tool | Role in Owed |
|---|---|
| **Senso** | Trusted context layer. Store policies are ingested per section. Every decision cites a Senso passage (`/org/search/context`, logged with content_id, kb_node_id, version_id, rank), and every claim is fact-checked before it is sent (`/org/evals/text`). |
| **Superlinked SIE** | `urchade/gliner_multi-v2.1` entity extraction on every receipt (cross-checked against the parser). `Qwen/Qwen3-Reranker-0.6B` reranks policy passages so the best clause is cited. The reranker is discovered from the server's model list. |
| **Arcee** | OpenAI-compatible API. The agent lists the models its key can use and routes: a small, fast model reads rules into JSON, a large model writes the claim. |
| **Cognee** | Long-term memory graph of every claim (`/api/v1/remember`). Before acting, the agent asks it what happened last time with that store (`/api/v1/search`, GRAPH_COMPLETION). |
| **Amplitude** | Every agent step is sent as an `[Agent] Tool Call` event. LLM calls are tracked through `amplitude-ai`. |
| **Statsig** | Gate `owed_require_approval` and dynamic config `owed_autonomy.max_auto_claim_usd` set how autonomous the agent is. |
| **Airbyte** | Gmail agent connector: reads receipt emails, sends the summary. |
| **SingleStore** | Stores cases and the full audit trail. You can query it with SQL. |

## Run it

**Hosted (free, no terminal):** [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Mertdem21/owed). Render reads `render.yaml`, installs with uv and starts the app. Then paste your keys under Environment -> Add from .env.


On a Mac: double-click `run_setup.command` (install, seed Senso, test keys, one agent run), then `start_app.command` (UI on http://localhost:8000). `test_keys.command` re-tests every key.

```bash
uv sync --extra sponsors          # if one sponsor SDK fails to install, remove it from pyproject.toml: everything falls back
cp .env.example .env              # add your keys (each integration is optional)
uv run python -m owed.cli smoke   # test every integration on its own
uv run python -m scripts.seed_senso          # upload store policies to Senso (once)
uv run python -m owed.cli run --reset        # run the agent in the terminal
uv run uvicorn owed.app:app --reload --port 8000   # UI on http://localhost:8000
```

Query the audit trail:

```bash
curl -s localhost:8000/api/sql -H 'content-type: application/json' \
  -d '{"sql":"SELECT case_id, step, provider, summary FROM audit_events ORDER BY id"}'
```

## Test inbox (edge cases)

`data/inbox/test_inbox.json` holds 17 fictional emails built to trip the agent up. Run it with
`uv run python -m tests.test_inbox` (compares every case with the expected result), with
`uv run python -m owed.cli run --reset --inbox test`, or in the browser by opening the app with `?inbox=test`.
It never touches Gmail.

| Email(s) | Situation | Expected |
|---|---|---|
| Keyboard, -$30 | price drop inside the 14-day window | claim filed and verified |
| Webcam, -$15 | price drop on the **last day** of the window | claim filed and verified |
| SSD, -$20 | window missed **by one day** | declined, cites the window |
| Speaker (Clearance), -$20 | clearance items excluded | declined, cites the exclusion |
| Tablet (open-box), -$30 | open-box items excluded | declined, cites the exclusion |
| 34" monitor, -$170 | above the $150 autonomy limit | waits for your approval |
| Down vest, -$30 | store has **no** price-adjustment policy | declined |
| Blanket, charged twice | duplicate charge reported after 60 days | declined |
| Bookshelf + lamp, $120 + $45 | two different charges (split payment) | not flagged |
| Socks ordered twice, 2 days apart | intentional reorder | not flagged |
| "URGENT: your $500 refund" | phishing from an unknown store | ignored |
| Newsletter | not a receipt | ignored |

Result: **10/10 correct, $45.00 recovered, 1 waiting for approval.**

## Demo data

The three stores (Northwind Electronics, Cascade Outfitters, Brightline Home), their policies, orders and the demo inbox are fictional. They live in `data/`. The mock store API (`/merchant/...`) validates every claim against its own rules, independently of the agent. Store actions are labelled as simulated in the audit trail. The earlier-claims history in `data/memory_seed.json` is fictional too; `reset_memory.command` (or `uv run python -m owed.cli memory-reset`) clears Cognee and loads it. The summary email to you is real when Gmail is connected. With Gmail connected, Owed reads your real inbox.
