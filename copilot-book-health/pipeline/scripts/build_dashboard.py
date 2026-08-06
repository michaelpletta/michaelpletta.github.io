#!/usr/bin/env python3
"""Build the Copilot Book Health Tracker dashboard from a data JSON file.

Usage:
    python build_dashboard.py <input.json> [output.html]

Input JSON shape (fields the agent fills from Kusto + optional WorkIQ):
{
  "meta": {
    "rep_name": "Cullen Hennessy", "segment": "MMAE / Corporate", "region": "AMER",
    "sales_owner_id": "0055c000009PxwsAAC", "as_of": "2026-07-28",
    "ubb_window": "June 1-28 vs July 1-28", "total_owned": 54
  },
  "book": {"seats_now":9750,"seats_1mo":10012,"seats_3mo":9924,"ubb_prev":504800,"ubb_cur":543114},
  "accounts": [
    {"account":"Conga","seats_now":1053,"seats_1mo":1001,"seats_3mo":751,
     "hwm":1053,"risk":217,"grr":0.99,"health":"Green",
     "prev_ubb":107738,"cur_ubb":146396,"ai_units":14639629,
     "net_prev":41000,"net_cur":58200,"engaged":612,          // net_*/engaged/pool_* optional (query 06)
     "projected_eom":162000,"pool_dollars":120000,"pool_util":1.35,
     "budget_target":100000,"budget_current":79789,"budget_limit_type":"PreventFurtherUsage", // budget_* optional (Odometer get_budgets, enterprise slug)
     "surfaces":{"IDE (chat/completions)":108274,"CLI / Agent":6469,"Code Review":3834}, // surfaces optional (query 07)
     "meetings_90d":6,"last_met":"2026-07-20"},   // meetings_* optional
    ...
  ]
}

net_delta / net_pct / util are computed here from net_prev/net_cur/engaged when absent.
book.net_prev / book.net_cur / book.engaged_now / book.overage_accounts are rolled up here
from the per-account fields, so the caller only fills the account rows.

seat_delta / seat_pct / ubb_delta / ubb_pct are computed here if absent, so the
merge step upstream can stay simple. Recommendations and the engagement correlation
are computed deterministically. Output is a single self-contained HTML file.
"""
from __future__ import annotations
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "templates", "dashboard-template.html")

sys.path.insert(0, HERE)
from recommend import recommend  # noqa: E402


def _f(v, d=0.0):
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


def enrich(acc: dict) -> dict:
    """Fill computed delta fields if the caller did not provide them."""
    a = dict(acc)
    seats_now = _f(a.get("seats_now"))
    seats_1mo = _f(a.get("seats_1mo"))
    prev_ubb = _f(a.get("prev_ubb", a.get("jun_ubb")))
    cur_ubb = _f(a.get("cur_ubb", a.get("jul_ubb")))
    a["seats_now"] = seats_now
    a["cur_ubb"] = cur_ubb
    a["prev_ubb"] = prev_ubb
    if "seat_delta" not in a:
        a["seat_delta"] = int(round(seats_now - seats_1mo))
    if "seat_pct" not in a:
        a["seat_pct"] = round((a["seat_delta"] / seats_1mo * 100) if seats_1mo > 0 else 0.0, 1)
    if "ubb_delta" not in a:
        a["ubb_delta"] = int(round(cur_ubb - prev_ubb))
    if "ubb_pct" not in a:
        a["ubb_pct"] = round(((cur_ubb - prev_ubb) / prev_ubb * 100) if prev_ubb > 0 else 0.0, 1)
    a.setdefault("grr", 1.0)
    a.setdefault("risk", 0)
    a.setdefault("hwm", seats_now)
    a.setdefault("health", "Unknown")
    a.setdefault("seats_3mo", None)
    a.setdefault("meetings_90d", None)
    a.setdefault("last_met", None)

    # Consumption-economics fields (net / engaged / utilization / pool). Optional: when the
    # burn-rate query is not merged in, these stay None/absent and the dashboard shows "-".
    a.setdefault("net_prev", None)
    a.setdefault("net_cur", None)
    a.setdefault("engaged", None)
    a.setdefault("projected_eom", None)
    a.setdefault("pool_dollars", None)
    a.setdefault("pool_util", None)

    # Real Odometer budget (enterprise-slug accounts only). Optional: absent for org-only /
    # non-enterprise accounts, in which case the dashboard shows "-" for the budget columns.
    a.setdefault("budget_target", None)
    a.setdefault("budget_current", None)
    a.setdefault("budget_limit_type", None)
    a.setdefault("budget_scope", None)
    if a.get("budget_pct") is None:
        bt = _f(a.get("budget_target"))
        a["budget_pct"] = round(_f(a.get("budget_current")) / bt, 3) if bt > 0 else None

    # Product / surface mix (query 07). `surfaces` is an object mapping product label -> gross $.
    # Optional: absent when query 07 is not merged in. Derive the single largest surface for the
    # table's "Top product" column.
    surfaces = a.get("surfaces")
    if isinstance(surfaces, dict) and surfaces:
        top = max(surfaces.items(), key=lambda kv: _f(kv[1]))
        a["top_surface"] = top[0]
    else:
        a.setdefault("surfaces", None)
        a.setdefault("top_surface", None)
    if a.get("net_cur") is not None:
        net_cur = _f(a.get("net_cur"))
        net_prev = _f(a.get("net_prev"))
        if "net_delta" not in a or a.get("net_delta") is None:
            a["net_delta"] = int(round(net_cur - net_prev))
        if "net_pct" not in a or a.get("net_pct") is None:
            a["net_pct"] = round(((net_cur - net_prev) / net_prev * 100) if net_prev > 0 else 0.0, 1)
    else:
        a.setdefault("net_delta", None)
        a.setdefault("net_pct", None)
    if a.get("util") is None:
        engaged = a.get("engaged")
        a["util"] = round(_f(engaged) / seats_now, 3) if (engaged is not None and seats_now > 0) else None
    return a


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def engagement(accounts: list[dict]) -> dict:
    pairs = [(a["meetings_90d"], a["cur_ubb"]) for a in accounts
             if a.get("meetings_90d") is not None and _f(a.get("cur_ubb")) > 0]
    if len(pairs) < 3:
        return {"enabled": False}
    r = pearson([p[0] for p in pairs], [p[1] for p in pairs])
    if r is None:
        return {"enabled": False}
    # Flag accounts that break the pattern for the rep to notice.
    high_ubb_no_mtg = [a["account"] for a in accounts
                       if _f(a.get("cur_ubb")) > 0 and (a.get("meetings_90d") or 0) == 0
                       and _f(a.get("cur_ubb")) >= _median([_f(x.get("cur_ubb")) for x in accounts])]
    note = (f"{'Positive' if r >= 0 else 'Negative'} association between meeting cadence and "
            f"consumption across {len(pairs)} accounts. ")
    if high_ubb_no_mtg:
        note += ("High-usage, zero-meeting accounts to re-engage: "
                 + ", ".join(high_ubb_no_mtg[:5]) + ".")
    return {"enabled": True, "correlation": round(r, 3), "note": note}


def _median(vals: list[float]) -> float:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return 0.0
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2


def build(data: dict) -> str:
    meta = data.get("meta", {})
    book = dict(data.get("book", {}))
    accounts = [enrich(a) for a in data.get("accounts", [])]
    recs = recommend(accounts)
    engage = engagement(accounts)

    # Book-level consumption-economics roll-up from the per-account net/engaged fields.
    # Only populated when at least one account carries burn-rate data; otherwise left absent
    # so the dashboard renders those KPIs as "-".
    net_accts = [a for a in accounts if a.get("net_cur") is not None]
    if net_accts:
        book.setdefault("net_cur", round(sum(_f(a.get("net_cur")) for a in net_accts)))
        book.setdefault("net_prev", round(sum(_f(a.get("net_prev")) for a in net_accts)))
    eng_accts = [a for a in accounts if a.get("engaged") is not None]
    if eng_accts:
        book.setdefault("engaged_now", round(sum(_f(a.get("engaged")) for a in eng_accts)))
    book.setdefault("overage_accounts",
                    sum(1 for a in accounts if a.get("pool_util") is not None and _f(a.get("pool_util")) >= 1.0))

    # Real-budget roll-up: accounts at or above 80% of a contracted Odometer budget.
    budget_accts = [a for a in accounts if a.get("budget_target") is not None and _f(a.get("budget_target")) > 0]
    if budget_accts:
        book.setdefault("budget_accounts", len(budget_accts))
        book.setdefault("budget_near_cap",
                        sum(1 for a in budget_accts if _f(a.get("budget_pct")) >= 0.80))

    # Product / surface mix roll-up: sum each account's surfaces into a book-level product mix.
    mix: dict[str, float] = {}
    for a in accounts:
        s = a.get("surfaces")
        if isinstance(s, dict):
            for label, dollars in s.items():
                mix[label] = mix.get(label, 0.0) + _f(dollars)
    if mix:
        book.setdefault("surfaces", {k: round(v) for k, v in sorted(mix.items(), key=lambda kv: -kv[1])})

    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        html = fh.read()

    subs = {
        "/*__META_JSON__*/{}": "/*META*/" + json.dumps(meta),
        "/*__BOOK_JSON__*/{}": "/*BOOK*/" + json.dumps(book),
        "/*__DATA_JSON__*/[]": "/*DATA*/" + json.dumps(accounts),
        "/*__RECS_JSON__*/[]": "/*RECS*/" + json.dumps(recs),
        "/*__ENGAGE_JSON__*/{enabled:false}": "/*ENGAGE*/" + json.dumps(engage),
    }
    for token, value in subs.items():
        if token not in html:
            raise SystemExit(f"Template token not found: {token}")
        html = html.replace(token, value)
    return html


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python build_dashboard.py <input.json> [output.html]")
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        data = json.load(fh)
    out = sys.argv[2] if len(sys.argv) > 2 else "copilot-book-health-tracker.html"
    html = build(data)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    n = len(data.get("accounts", []))
    print(f"Wrote {out} ({n} accounts)")


if __name__ == "__main__":
    main()
