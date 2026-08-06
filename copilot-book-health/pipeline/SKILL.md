---
name: copilot-book-health
description: "Build a personal Copilot Book Health Tracker for a sales rep: verify the rep's identity, resolve their owned accounts, pull seat and UBB (usage-based) telemetry from Kusto, compute month-over-month growth and churn, generate a self-contained HTML dashboard, and produce deterministic where-to-spend-time recommendations. Optionally correlates meeting cadence (calendar) with consumption. USE FOR: my book health, Copilot book review, my growth accounts, seat and UBB MoM for my accounts, churn watchlist, where should I spend my time, rep dashboard, book health tracker. DO NOT USE FOR: a single named customer deep-dive (use customer-research), Salesforce pipeline/opportunity detail (use salesforce-context), drafting emails (use delegation)."
---

# Copilot Book Health Tracker

Turn a rep's owned book into a single HTML dashboard plus a ranked, deterministic action list. Everything is scoped to the accounts the signed-in rep owns, driven by live Kusto telemetry, and reproducible: the same numbers always yield the same recommendations.

This skill is designed to scale to every rep. Nothing about it is hardcoded to one person; the rep's identity is resolved at run time.

## What it produces

1. `copilot-book-health-tracker.html` (repo root by default) — a self-contained dashboard: book KPIs, seat and UBB MoM charts, churn watchlist, health mix, a Recommendations panel, an optional engagement scatter, and a sortable/filterable account table.
2. A short markdown briefing in chat: headline, top growth accounts, churn watchlist, and the top recommendations.

## Prerequisites (auth)

Both token sets expire overnight. If a step returns an auth error, have the rep re-authenticate, then resume.

| Data source | Re-auth |
|-------------|---------|
| Salesforce (owner lookup) | `./scripts/sf-reauth.sh` or https://revenue-mcp-server.githubapp.com/auth |
| Azure / Kusto (telemetry) | `az login --use-device-code` with the rep's `@githubazure.com` account |
| WorkIQ (optional calendar) | Interactive M365 auth on first WorkIQ call |

Tools used: `revenue-mcp-server-query_kusto` (database `rev_source`), `revenue-mcp-server-query_salesforce`, and WorkIQ (`workiq-ask`) for the optional engagement phase.

## Files in this skill

- `queries/01-resolve-owner.kql` — resolve `sales_owner_id` + segment/team/region
- `queries/02-book-headline.kql` — book-level KPIs (seats now/1mo/3mo, UBB prev/cur)
- `queries/03-seat-mom.kql` — per-account seat MoM + churn signals
- `queries/04-ubb-mom.kql` — per-account UBB (consumption) MoM
- `queries/06-net-caps-util.kql` — per-account net (billable) spend, engaged users, and pool / overage risk
- `queries/07-surface-mix.kql` — per-account Copilot consumption split by product / surface (Chat, CLI / Agent, Code Review, third-party, App, Mobile)
- `queries/05-engagement-correlation.md` — WorkIQ calendar attribution (optional)
- `scripts/recommend.py` — deterministic 6-bucket recommendation engine
- `scripts/build_dashboard.py` — merges data, computes deltas + correlation, renders HTML
- `templates/dashboard-template.html` — the reusable dashboard shell

---

## Phase 1 — Verify identity

Goal: know exactly who the rep is and get their `sales_owner_id`.

1. Read `.github/user-identity.yml`. Use `name`, `email`, and `github_username` if present.
2. If email/name is missing, ask the rep for their GitHub email (one question, via `ask_user`). Do not guess.
3. Resolve the Salesforce User Id:
   ```sql
   SELECT Id, Name, Email FROM User WHERE Email = '<rep email>' LIMIT 1
   ```
4. Confirm the owner exists in telemetry by running `queries/01-resolve-owner.kql` with `{{OWNER_ID}}` = the Salesforce User Id (and `{{GH_USERNAME}}` as a fallback match). Capture `sales_owner`, `sales_segment`, `sales_team`, `region` for the dashboard header.

If no rows return, stop and tell the rep their owner id was not found in `sales_owner_dim` (likely wrong email or a non-quota role). Do not fall back to an unscoped book.

## Phase 2 — Pull the data

All queries run against `rev_source`. Substitute `{{OWNER_ID}}` with the resolved `sales_owner_id` in every query.

1. **Headline** — run `queries/02-book-headline.kql`. Set the UBB windows first (see the caveat below).
2. **Seat MoM** — run `queries/03-seat-mom.kql`. One row per account: `seats_now/1mo/3mo`, `seat_delta`, `seat_pct`, `hwm`, `risk`, `grr`, `health`.
3. **UBB MoM** — run `queries/04-ubb-mom.kql`. One row per account: `prev_ubb`, `cur_ubb`, `ubb_delta`, `ubb_pct`, `ai_units`.
4. **Net / caps / utilization** — run `queries/06-net-caps-util.kql` (same `{{OWNER_ID}}` and equal-length windows). One row per account: `net_prev`, `net_cur`, `net_delta`, `net_pct`, `engaged`, `projected_eom`, `pool_dollars`, `pool_util`. This adds the billable (net) consumption, distinct engaged users, and included-pool / overage-risk signals the gross-only view misses.
5. **Product / surface mix** — run `queries/07-surface-mix.kql` (same `{{OWNER_ID}}`, current window only). Returns one row per `(salesforce_account_id, surface)` with gross `$`. Pivot the rows for each account into a `surfaces` object, e.g. `{"IDE (chat/completions)": 108274, "CLI / Agent": 6469, "Code Review": 3834}`. This answers which Copilot products each account consumes (IDE Chat/completions vs CLI / coding agent vs Code Review vs third-party clients vs App vs Mobile).
6. **Real budgets / caps (optional, ask first)** — before running this step, ask the rep whether to pull contracted budget data, because it is materially slower (two Odometer/Salesforce lookups per enterprise account across the whole book). Use `ask_user` with two choices:
   - **Lightweight option — no budget information** (faster): skip this step. The dashboard hides the budget columns and cap-watch card, and the OVERAGE recommendation bucket falls back to the modeled `pool_util`. Everything else is identical.
   - **Slower option — budget information** (complete): run this step. For each account that has an enterprise GitHub slug (from `get_salesforce_account` → `github_accounts[].slug` with `namespace: "enterprise"`), call the Odometer `get_budgets` tool (`namespace: "enterprise"`, `slug: <slug>`). Take the **Enterprise-scope** AI/Copilot row (`resource_type == "Enterprise"`, preferring `budget_name` of `All AI Credit SKUs`, then `Copilot AI Credits`, then `Copilot`; ignore Actions / Codespaces / Packages / Git LFS rows) and set `budget_target` (target_amount), `budget_current` (current_amount), and `budget_limit_type` (`PreventFurtherUsage` = hard cap, `AlertingOnly` = alert cap). This is the contracted cap and enforcement posture, unlike the modeled `pool_util`. Org-only / non-enterprise accounts have no enterprise AI budget; leave the fields absent.
7. Join seat, UBB, net/caps/util, surface-mix, and (if pulled) budget rows on `salesforce_account_id` into one account list.

Data caveats (state these in the briefing):
- `assigned_users` is the Copilot seat source (the ARR/seats fact does not carry Copilot). `gross_usage_in_dollar` is usage-side, not final billed revenue.
- `account_billable_usage_in_dollar` (net) is usage AFTER the included promo/standard pool, still usage-side and NOT final billed consumption; the month-end customer discount is not in this dataset. Treat net as directional.
- `pool_util` (`promo_pool_utilization_pct`) is projected month-end spend divided by a MODELED included-spend pool, not a contracted cap. At or above 1.0 the account is projected into overage. It is a planning estimate; where a real budget exists (below), prefer the budget for the headline message, but the modeled pool can also flag overage on its own.
- `budget_target` / `budget_current` / `budget_limit_type` come from Odometer `get_budgets` and ARE the contracted budget and enforcement posture for enterprise-slug accounts. `PreventFurtherUsage` means usage is blocked at the limit (hard cap); `AlertingOnly` means usage continues past the limit with alerts. The OVERAGE bucket fires when the real budget is at or above 80% consumed OR the modeled `pool_util` projects overage; when a real budget is present it drives the headline message and the play names the enforcement posture. Budgets exist only for enterprise-namespace accounts; leave the fields absent elsewhere.
- `surfaces` (query 07) is gross usage-side `$` grouped by the Copilot `integration` (surface). The `integration` label is evolving, so the query buckets by pattern; treat the split as directional product mix, not exact SKU billing.
- `engaged` (`account_nbr_of_ubb_users`) is distinct billable Copilot users; utilization = engaged / assigned seats. Low utilization signals shelfware.
- Query 06 draws from `salesforce_account_daily_ubb_utilization_burn_rate_fact`. If it returns nothing, omit those fields; the builder and dashboard degrade gracefully (net/engaged/util/pool columns and KPIs show "-"). The same graceful degradation applies to `surfaces` (query 07) and the `budget_*` fields.
- UBB history begins around 2026-06, and the current month is usually partial. Compare **equal-length** windows (e.g. previous month 1-28 vs current month 1-28), not full-month vs partial-month. Set `{{PREV_START}}/{{PREV_END}}/{{CUR_START}}/{{CUR_END}}` accordingly.
- `latest` is a reserved token in KQL; the queries use `asof` instead.

## Phase 3 — Build the dashboard

1. Assemble a data JSON file:
   ```json
   {
     "meta": {"rep_name":"...","segment":"...","region":"...","sales_owner_id":"...","as_of":"YYYY-MM-DD","ubb_window":"Jun 1-28 vs Jul 1-28","total_owned":54},
     "book": {"seats_now":0,"seats_1mo":0,"seats_3mo":0,"ubb_prev":0,"ubb_cur":0},
     "accounts": [ { "account":"...","seats_now":0,"seats_1mo":0,"seats_3mo":0,"hwm":0,"risk":0,"grr":1.0,"health":"Green","prev_ubb":0,"cur_ubb":0,"ai_units":0,"net_prev":0,"net_cur":0,"engaged":0,"projected_eom":0,"pool_dollars":0,"pool_util":0.0,"budget_target":0,"budget_current":0,"budget_limit_type":"PreventFurtherUsage","surfaces":{"IDE (chat/completions)":0,"CLI / Agent":0,"Code Review":0} } ]
   }
   ```
   `seat_delta/seat_pct/ubb_delta/ubb_pct/net_delta/net_pct/util` are optional; the builder computes them if absent. The `net_*`, `engaged`, `projected_eom`, `pool_dollars`, and `pool_util` fields are optional too; omit them if query 06 is skipped and the dashboard hides those metrics. The `budget_*` fields (Odometer) and the `surfaces` object (query 07) are also optional and hide their KPIs/columns/cards when absent. The builder rolls `book.net_cur/net_prev/engaged_now/overage_accounts/budget_accounts/budget_near_cap/surfaces` up from the account rows, so you only fill the account rows.
2. Run the builder:
   ```bash
   python3 .github/skills/copilot-book-health/scripts/build_dashboard.py <data.json> copilot-book-health-tracker.html
   ```
3. Serve and open it for the rep:
   ```bash
   python3 -m http.server 8777   # detached, from repo root
   ```
   Open the `browser` canvas on `http://localhost:8777/copilot-book-health-tracker.html`.

The builder replaces five tokens in the template (`__META_JSON__`, `__BOOK_JSON__`, `__DATA_JSON__`, `__RECS_JSON__`, `__ENGAGE_JSON__`). It fails loudly if a token is missing, so the output is always fully populated or nothing.

## Phase 4 — Deterministic recommendations

`scripts/recommend.py` assigns every account to exactly one bucket (first match wins) using fixed, auditable thresholds. `build_dashboard.py` calls it automatically; you can also run it standalone (`python3 recommend.py < data.json`).

| Bucket | Trigger (simplified) | Play |
|--------|----------------------|------|
| SAVE | GRR30 < 0.90, or health Red, or a material seat drop (≤ -10% MoM, or ≤ -25 seats and ≤ -5%) | Executive save motion; root-cause; written renewal plan |
| INVESTIGATE | Consumption down ≥ 20% MoM while seats held | Find the teams that went quiet before it hits renewal |
| OVERAGE | Real budget ≥ 80% consumed (Odometer), or projected pool utilization ≥ 100% with billable net spend > 0 | True up committed consumption or lift the budget before usage is throttled; a hard cap (PreventFurtherUsage) is more urgent than an alert cap |
| EXPAND | Seats up and consumption up ≥ 15%, GRR ≥ 0.90, not Red | Propose seat true-up / tier upgrade now |
| DEEPEN | Utilization < 40% (with ≥ 25 seats), or ≥ 25% of seats at risk, or seats stable but consumption flat (0–15%) | Enablement / champion activation so seats convert to usage |
| MONITOR | Everything else | No action this cycle |

Buckets are evaluated in the order above (first match wins). OVERAGE sits below the churn signals (SAVE, INVESTIGATE) because retention outranks an upsell, but above EXPAND because an account already spilling past its budget or included pool is time-sensitive revenue. OVERAGE fires when a real Odometer budget is at or above 80% consumed OR the modeled `pool_util` projects overage (>= 1.0 with billable net spend); when a real budget is present it drives the message and the play names the enforcement posture. Utilization (engaged / seats) feeds the DEEPEN shelfware trigger. OVERAGE and the utilization trigger only fire when the relevant data (query 06 and/or budgets) is present; without it the engine behaves exactly as the seat/gross-only version.

Product-surface mix (query 07) reports which Copilot products each account consumes: IDE Chat/completions, CLI / coding agent, Code Review, third-party clients, App, and Mobile, shown as a book-level product-mix doughnut and a per-account "Top product" column. The coding-agent AI-unit split at finer granularity and GHAS committer depth are still not reported at the Salesforce-account grain in `rev_source`; use `customer-research` for a single-account feature deep-dive.

Accounts are ranked by an attention score (bucket weight first, then dollars at stake, then seat count), so the top of the list is where the hour is best spent. Thresholds live as named constants at the top of `recommend.py`; changing them changes every rep's output consistently.

## Phase 5 — Engagement tracker (time invested vs UBB)

The dashboard's account table and Recommendations panel are the standing tracker: revisit weekly, watch DEEPEN/EXPAND accounts move as consumption responds to the time invested. Because `cur_ubb` (consumption) is shown next to each recommendation, the rep can see whether attention is translating into usage-based growth over successive refreshes.

## Phase 6 — Calendar correlation (optional feature)

This phase answers "do the accounts I meet with more actually consume more?" It is optional and degrades gracefully: if calendar data is unavailable, the dashboard simply hides the engagement scatter (`ENGAGE.enabled = false`).

1. Follow `queries/05-engagement-correlation.md`: use WorkIQ to pull the rep's last ~90 days of calendar events, match attendee domains / subjects to book accounts, and count meetings per account (`meetings_90d`, optional `last_met`).
2. Add `meetings_90d` to the relevant account records in the data JSON and rebuild.
3. `build_dashboard.py` computes a Pearson correlation (stdlib only) between `meetings_90d` and `cur_ubb` when at least three accounts have meeting counts, and flags high-usage/zero-meeting accounts to re-engage.

Treat correlation as directional, not causal, and say so. Attendee-to-account matching is heuristic; note it in the briefing.

---

## Error handling

| Symptom | Cause | Action |
|---------|-------|--------|
| Salesforce query returns auth error | SFDC token expired | Re-auth (`./scripts/sf-reauth.sh`), retry |
| Kusto query returns auth error | Azure token expired | `az login --use-device-code`, retry |
| `01-resolve-owner` returns no rows | Wrong email or non-quota role | Confirm email with rep; do not use an unscoped book |
| `SYN0002` on a query | A let-binding named `latest` | Rename it; the shipped queries already use `asof` |
| UBB MoM looks inflated/deflated | Full vs partial month compared | Use equal-length windows in queries 02, 04, and 06 |
| Net / util / pool columns show "-" | Query 06 skipped or returned no rows | Optional; run `queries/06-net-caps-util.kql` and merge its fields to populate them |
| Budget columns / cap-watch card empty | No enterprise slug, or budget step skipped | Expected for org-only accounts; for enterprise slugs call Odometer `get_budgets` and merge `budget_target/current/limit_type` |
| Product-mix card / Top product empty | Query 07 skipped or `surfaces` not merged | Optional; run `queries/07-surface-mix.kql` and pivot its rows into each account's `surfaces` object |
| Engagement scatter missing | < 3 accounts with meeting data, or Phase 6 skipped | Expected; dashboard hides the section |
| Builder aborts "Template token not found" | Template edited | Restore the five `__*_JSON__` tokens in the template |

## Style

Follow repo writing rules in all briefings and generated text: no em-dashes, no emojis, no hyperbole. State numbers plainly and cite the as-of date and windows used.
