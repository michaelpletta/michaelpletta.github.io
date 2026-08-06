# Copilot Book Health - daily pipeline

Regenerates the team Copilot Book Health dashboard and republishes it to GitHub Pages at a
stable URL, then files a GitHub issue with the link. Driven daily by a Copilot scheduled workflow.

Stable dashboard URL (never changes): https://michaelpletta.github.io/copilot-book-health/

## Inputs
- `config.json` - owner IDs (6-person team), publish target, issue repo, dashboard URL.
- `queries/team-consolidated.kql` - one row per account: seats (now/1mo/3mo), churn signals,
  gross UBB (prev/cur), AI units, net spend, engaged users, pool/overage.
- `queries/team-surface-mix.kql` - per-account Copilot product/surface split (current window).
- `scripts/transform.py` - merges the two Kusto payloads into `data.json`.
- `scripts/build_dashboard.py` + `scripts/recommend.py` + `templates/dashboard-template.html`
  - render the self-contained dashboard HTML.

## Window rule
UBB history starts ~2026-06 and the running month is partial, so always compare EQUAL-LENGTH
month-to-date windows: the two most recent COMPLETE months, day 1-28 each
(e.g. run in Aug -> `Jun 1-28` vs `Jul 1-28`). `as_of` = health-score max date.

## Daily steps (run by the workflow agent)
1. Read `config.json`. Build the KQL `owners` array from `owner_ids`.
2. Pick windows per the rule above (two most recent complete months, 1-28).
3. Run `queries/team-consolidated.kql` via `revenue-mcp-server-query_kusto` (database `rev_source`),
   substituting the owners array and the four window dates. Save the JSON payload to `consolidated.json`.
4. Run `queries/team-surface-mix.kql` the same way (current window only). Save to `surface.json`.
5. `python3 scripts/transform.py consolidated.json surface.json data.json --as-of <asof> --window "<label>"`.
6. `python3 scripts/build_dashboard.py data.json index.html`.
7. Publish `index.html` to `config.publish_repo` at `config.publish_path` on `config.publish_branch`
   (GitHub Contents API PUT with the existing file sha). `.nojekyll` at repo root keeps Pages from
   trying to run Jekyll on the dashboard (its JS contains `{{`/`{%` that break the Jekyll build).
8. Create a GitHub issue in `config.issue_repo` titled `Copilot Book Health - <YYYY-MM-DD>` whose body
   contains the dashboard link plus a short headline (book seats, UBB prev->cur, top SAVE/EXPAND/OVERAGE).

## Auth
- Kusto: Azure token via the revenue MCP (re-auth `az login --use-device-code` if it 401s).
- Publish + issue: `gh` / `GH_TOKEN` (needs `repo` scope; already present).
- Python: `~/.local/bin/python3` (standalone CPython, stdlib only).
