#!/usr/bin/env python3
"""Merge the team consolidated Kusto pull + surface-mix into build_dashboard.py's data.json.

Usage:
  python3 transform.py <consolidated.json> <surface.json> <out data.json> \
      --as-of YYYY-MM-DD --window "Jun 1-28 vs Jul 1-28"

The two input files are the raw query_kusto JSON payloads ({"columns":[...],"rows":[...]}).
Meta rep_name/segment/region come from config.json in the same directory.
"""
import json, sys, os, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))


def load_rows(path):
    d = json.load(open(path))
    cols = d["columns"]
    return [r if isinstance(r, dict) else dict(zip(cols, r)) for r in d["rows"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("consolidated")
    ap.add_argument("surface")
    ap.add_argument("out")
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--window", required=True)
    a = ap.parse_args()

    cons = load_rows(a.consolidated)
    surf = load_rows(a.surface)

    smap = {}
    for s in surf:
        smap.setdefault(s["salesforce_account_id"], {})[s["surface"]] = s["gross"]

    accounts = []
    for r in cons:
        aid = r["salesforce_account_id"]
        acc = {
            "account": r.get("account") or "(unnamed)",
            "rep": r.get("rep") or "Unknown",
            "seats_now": r.get("seats_now", 0),
            "seats_1mo": r.get("seats_1mo", 0),
            "seats_3mo": r.get("seats_3mo", 0),
            "hwm": r.get("hwm", 0),
            "risk": r.get("risk", 0),
            "grr": r.get("grr", 1.0),
            "health": r.get("health") or "Unknown",
            "prev_ubb": r.get("prev_ubb", 0.0),
            "cur_ubb": r.get("cur_ubb", 0.0),
            "ai_units": r.get("ai_units", 0.0),
            "net_prev": r.get("net_prev", 0.0),
            "net_cur": r.get("net_cur", 0.0),
            "engaged": r.get("engaged", 0),
            "projected_eom": r.get("projected_eom", 0.0),
            "pool_dollars": r.get("pool_dollars", 0.0),
            "pool_util": r.get("pool_util", 0.0),
        }
        if aid in smap:
            acc["surfaces"] = smap[aid]
        accounts.append(acc)

    def s(field):
        return round(sum(float(x.get(field) or 0) for x in accounts))

    book = {
        "seats_now": s("seats_now"),
        "seats_1mo": s("seats_1mo"),
        "seats_3mo": s("seats_3mo"),
        "ubb_prev": s("prev_ubb"),
        "ubb_cur": s("cur_ubb"),
    }
    meta = {
        "rep_name": CFG["rep_name"],
        "segment": CFG["segment"],
        "region": CFG["region"],
        "sales_owner_id": "%d Salesforce owners" % len(CFG["owner_ids"]),
        "as_of": a.as_of,
        "ubb_window": a.window,
        "total_owned": len(accounts),
    }
    json.dump({"meta": meta, "book": book, "accounts": accounts}, open(a.out, "w"))
    print("Wrote %s: %d accounts | seats_now=%d ubb_prev=%d ubb_cur=%d"
          % (a.out, len(accounts), book["seats_now"], book["ubb_prev"], book["ubb_cur"]))


if __name__ == "__main__":
    main()
