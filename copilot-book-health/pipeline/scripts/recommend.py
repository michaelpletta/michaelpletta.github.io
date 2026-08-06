#!/usr/bin/env python3
"""Deterministic recommendation engine for the Copilot Book Health Tracker.

Given a list of per-account records, assign each account to exactly one action
bucket using fixed, transparent rules (no model judgment), attach a plain-English
reason and recommended play, and compute an attention score for time allocation.

Buckets (evaluated in priority order; first match wins):
  1. SAVE        - active contraction / renewal risk
  2. INVESTIGATE - seats hold but usage is falling (silent disengagement)
  3. OVERAGE     - projected to exceed the contracted budget or the included consumption pool (true-up / budget)
  4. EXPAND      - seats AND usage both rising, healthy
  5. DEEPEN      - seats not converting to usage / low utilization / high at-risk ratio
  6. MONITOR     - stable, no action needed

The rules are intentionally simple and auditable so every rep gets the same
recommendation from the same numbers.
"""
from __future__ import annotations
from typing import Any

# Tunable thresholds. Keep these visible so the logic stays auditable.
BIG_SEAT_DROP_PCT = -10.0     # seat_pct at or below this = material contraction
BIG_SEAT_DROP_ABS = -25       # absolute seat loss this large ...
MIN_PCT_FOR_ABS_DROP = -5.0   # ... but only if it is also at least a -5% move (protects large books)
LOW_GRR = 0.90                # 30-day gross retention below this = at risk
UBB_UP_PCT = 15.0             # consumption growth at or above this = expanding
UBB_DOWN_PCT = -20.0          # consumption drop at or below this = disengaging
RISK_RATIO = 0.25            # at-risk users / seats at or above this = adoption gap
CONTRACTION_FRAC = 0.90       # seats_now below 90% of 180d high-watermark = off peak
OVERAGE_POOL_UTIL = 1.0       # projected pool utilization at or above this = projected overage
BUDGET_NEAR = 0.80            # real Odometer budget consumed at or above this = true-up conversation
LOW_UTIL = 0.40               # engaged users / seats below this (with enough seats) = shelfware
MIN_SEATS_FOR_UTIL = 25       # only judge utilization once a book has a meaningful seat count

BUCKET_WEIGHT = {"SAVE": 6, "INVESTIGATE": 5, "OVERAGE": 4, "EXPAND": 3, "DEEPEN": 2, "MONITOR": 1}
BUCKET_LABEL = {
    "SAVE": "Save / intervene",
    "INVESTIGATE": "Investigate",
    "OVERAGE": "Overage / true-up",
    "EXPAND": "Expand",
    "DEEPEN": "Deepen adoption",
    "MONITOR": "Monitor",
}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def classify(a: dict) -> dict:
    """Return a recommendation dict for a single account record.

    Delta fields (seat_delta/seat_pct/ubb_pct) are derived from the raw
    seat/UBB fields when absent, so this function gives the same answer whether
    or not an upstream step precomputed them.
    """
    seats_now = _num(a.get("seats_now"))
    seats_1mo = _num(a.get("seats_1mo"))
    prev_ubb = _num(a.get("prev_ubb", a.get("jun_ubb")))
    cur_ubb = _num(a.get("cur_ubb", a.get("jul_ubb")))

    seat_delta = _num(a.get("seat_delta")) if a.get("seat_delta") is not None \
        else seats_now - seats_1mo
    seat_pct = _num(a.get("seat_pct")) if a.get("seat_pct") is not None \
        else ((seat_delta / seats_1mo * 100) if seats_1mo > 0 else 0.0)
    ubb_pct = _num(a.get("ubb_pct")) if a.get("ubb_pct") is not None \
        else (((cur_ubb - prev_ubb) / prev_ubb * 100) if prev_ubb > 0 else 0.0)
    grr = _num(a.get("grr"), 1.0)
    health = (a.get("health") or "").strip().title()
    risk = _num(a.get("risk"))
    hwm = _num(a.get("hwm"))

    # New consumption-economics signals (usage-side; graceful when absent).
    net_cur = _num(a.get("net_cur"))
    engaged = a.get("engaged")
    pool_util = a.get("pool_util")
    pool_util_v = _num(pool_util)
    projected_eom = _num(a.get("projected_eom"))
    pool_dollars = _num(a.get("pool_dollars"))
    util = (engaged / seats_now) if (engaged is not None and seats_now > 0) else None

    # Real Odometer budget (enterprise-slug accounts only; graceful when absent). This is the
    # CONTRACTED cap and its enforcement posture, unlike the modeled pool above.
    budget_target = _num(a.get("budget_target"))
    budget_current = _num(a.get("budget_current"))
    budget_limit_type = (a.get("budget_limit_type") or "").strip()
    budget_pct = a.get("budget_pct")
    if budget_pct is None and budget_target > 0:
        budget_pct = budget_current / budget_target
    budget_pct_v = _num(budget_pct)
    has_budget = a.get("budget_target") is not None and budget_target > 0
    hard_cap = budget_limit_type.lower() == "preventfurtherusage"

    risk_ratio = (risk / seats_now) if seats_now > 0 else 0.0
    off_peak = hwm > 0 and seats_now < hwm * CONTRACTION_FRAC
    big_seat_drop = seat_pct <= BIG_SEAT_DROP_PCT or (
        seat_delta <= BIG_SEAT_DROP_ABS and seat_pct <= MIN_PCT_FOR_ABS_DROP)
    ubb_up = ubb_pct >= UBB_UP_PCT
    ubb_down = ubb_pct <= UBB_DOWN_PCT
    growing_seats = seat_delta > 0
    # Overage / true-up: flag when the REAL contracted budget is near its cap OR when the
    # modeled included-pool projection shows overage. Either can fire independently; when a
    # real budget is present its message takes priority (see the OVERAGE branch below).
    budget_overage = has_budget and budget_pct_v >= BUDGET_NEAR
    pool_overage = pool_util is not None and pool_util_v >= OVERAGE_POOL_UTIL and net_cur > 0
    overage_risk = budget_overage or pool_overage
    low_util = (util is not None and util < LOW_UTIL and seats_now >= MIN_SEATS_FOR_UTIL)

    reasons: list[str] = []

    # 1. SAVE
    if grr < LOW_GRR or health == "Red" or big_seat_drop:
        # "Red" is a composite health flag, so translate it into the specific,
        # quantified signals driving it rather than restating the color. Only
        # surface signals that are actually negative (retention below threshold,
        # seat contraction, falling consumption, at-risk seats, below-peak). We do
        # not cite rising consumption here because it is not a reason for concern.
        ev: list[str] = []
        if grr < LOW_GRR:
            ev.append(f"retention has fallen to {grr:.0%} of seats (healthy is {LOW_GRR:.0%} or higher)")
        if big_seat_drop:
            ev.append(f"seats fell {abs(seat_pct):.0f}% this month ({seat_delta:+.0f})")
        if ubb_down:
            ev.append(f"usage fell {abs(ubb_pct):.0f}% this month")
        if risk_ratio >= RISK_RATIO or (health == "Red" and seats_now > 0):
            ev.append(f"{risk_ratio:.0%} of seats are at risk ({int(risk)} of {int(seats_now)})")
        if off_peak:
            ev.append(f"seats are {1 - seats_now / hwm:.0%} below their 180-day high of {int(hwm)}")
        # De-duplicate while preserving order.
        seen: set[str] = set()
        ev = [x for x in ev if not (x in seen or seen.add(x))]
        if health == "Red":
            reasons.append("flagged red because " + ", ".join(ev)
                           if ev else "flagged red because retention and risk signals are elevated")
        else:
            reasons.extend(ev)
        bucket = "SAVE"
        # Tailor the action to the dominant driver so two red accounts don't read
        # identically. Priority order matches the branches below: catastrophic seat
        # loss, deep churn, heavy shelfware (>= 60% at risk), material seat drop,
        # falling usage, then the general at-risk case. Numbers are pulled from this
        # account's signals.
        if seat_pct <= -50:
            play = (f"What to do: seats collapsed {abs(seat_pct):.0f}% this month ({seat_delta:+.0f}). "
                    "Escalate to the economic buyer now to confirm whether this is a reorg, a budget "
                    "cut, or a switch to a competitor, then agree a written recovery plan.")
        elif grr < 0.50:
            play = (f"What to do: retention cratered to {grr:.0%}. Pull the usage-by-team breakdown to "
                    "see exactly who dropped off, then run an executive-sponsored win-back with the "
                    "remaining active users before renewal.")
        elif risk_ratio >= 0.60:
            play = (f"What to do: {risk_ratio:.0%} of seats ({int(risk)} of {int(seats_now)}) are inactive. "
                    "This is shelfware that will get cut at renewal. Line up enablement for the dormant "
                    "users now and an executive value review on what has actually been delivered.")
        elif big_seat_drop:
            play = (f"What to do: seats fell {abs(seat_pct):.0f}% ({seat_delta:+.0f}). Meet the buyer "
                    "this week to find the cause and put a written plan against the renewal.")
        elif ubb_down:
            play = (f"What to do: usage dropped {abs(ubb_pct):.0f}% this month. Find the teams that went "
                    "quiet and re-engage their leads before it lands in the renewal number.")
        elif risk_ratio >= RISK_RATIO:
            play = (f"What to do: {risk_ratio:.0%} of seats are inactive, so this is shelfware heading "
                    "into renewal. Schedule enablement for the dormant users and an executive value "
                    "review on what has been delivered.")
        else:
            play = ("What to do: book an executive health review this week, find the root cause of "
                    "the drop, and put a written renewal-save plan in place.")
    # 2. INVESTIGATE (seats not in freefall, but usage falling)
    elif ubb_down and seat_delta >= 0:
        reasons.append(f"usage fell {abs(ubb_pct):.0f}% this month even though the seat count held steady")
        if off_peak:
            reasons.append("seats are still below their 180-day high")
        bucket = "INVESTIGATE"
        play = ("What to do: find which teams stopped using Copilot and re-engage their champions "
                "before it shows up at renewal.")
    # 3. OVERAGE (on track to exceed the contracted budget or the included consumption pool)
    elif overage_risk:
        if budget_overage:
            posture = "a hard cap, so usage is blocked at the limit" if hard_cap \
                else "an alert-only cap, so usage keeps running past the limit"
            reasons.append(
                f"{budget_pct_v*100:.0f}% of the ${budget_target:,.0f} budget is already used"
                f" (${budget_current:,.0f}), and it is {posture}")
            if hard_cap:
                play = ("What to do: true up committed consumption or raise the hard cap before usage "
                        "is blocked at the limit, and confirm the cap type with the customer.")
            else:
                play = ("What to do: true up committed consumption or raise the budget before spend "
                        "runs past the agreed target, and confirm the cap type with the customer.")
        else:
            reasons.append(f"on track to use {pool_util_v*100:.0f}% of the included usage pool this month")
            if projected_eom > 0 and pool_dollars > 0:
                reasons.append(f"month-end run rate is about ${projected_eom:,.0f} against a ${pool_dollars:,.0f} pool")
            play = ("What to do: true up committed consumption or lift the budget so usage is not "
                    "throttled, and confirm the cap type with the customer.")
        bucket = "OVERAGE"
    # 4. EXPAND
    elif growing_seats and ubb_up and grr >= LOW_GRR and health != "Red":
        reasons.append(f"seats grew {seat_pct:+.0f}% and usage grew {ubb_pct:+.0f}% this month")
        bucket = "EXPAND"
        play = ("What to do: propose a seat true-up or tier upgrade now, while both adoption and "
                "usage are climbing.")
    # 5. DEEPEN
    elif low_util or risk_ratio >= RISK_RATIO or (seat_delta >= 0 and 0 <= ubb_pct < UBB_UP_PCT):
        if low_util:
            reasons.append(f"only {util*100:.0f}% of paid seats are actually being used ({int(engaged)} of {seats_now:.0f})")
        elif risk_ratio >= RISK_RATIO:
            reasons.append(f"{risk_ratio*100:.0f}% of seats are flagged at risk")
        else:
            reasons.append("seats are steady but usage is flat")
        bucket = "DEEPEN"
        play = ("What to do: run enablement and activate champions with the at-risk users so paid "
                "seats turn into real usage.")
    # 6. MONITOR
    else:
        reasons.append("steady on seats and usage")
        bucket = "MONITOR"
        play = "No action needed this cycle; recheck at the next refresh."

    # Attention score: bucket dominates, then dollar magnitude at stake, then seat count.
    attention = BUCKET_WEIGHT[bucket] * 1_000_000 + cur_ubb + seats_now
    # Assemble the "why" as one clean, capitalized sentence ending in a period.
    why = "; ".join(reasons)
    if why:
        why = why[:1].upper() + why[1:]
        if why[-1] not in ".!?":
            why += "."
    return {
        "account": a.get("account"),
        "bucket": bucket,
        "bucket_label": BUCKET_LABEL[bucket],
        "why": why,
        "play": play,
        "attention": round(attention, 0),
        "cur_ubb": cur_ubb,
        "cur_net": net_cur if a.get("net_cur") is not None else None,
        "util": round(util, 3) if util is not None else None,
        "pool_util": pool_util_v if pool_util is not None else None,
        "budget_pct": round(budget_pct_v, 3) if has_budget else None,
        "budget_limit_type": budget_limit_type or None,
        "top_surface": a.get("top_surface"),
        "seats_now": seats_now,
        "health": health or "Unknown",
    }


def recommend(accounts: list[dict]) -> list[dict]:
    """Classify every account and return recommendations sorted by attention (desc)."""
    recs = [classify(a) for a in accounts]
    recs.sort(key=lambda r: r["attention"], reverse=True)
    return recs


if __name__ == "__main__":
    import json
    import sys
    data = json.load(sys.stdin)
    accounts = data["accounts"] if isinstance(data, dict) else data
    json.dump(recommend(accounts), sys.stdout, indent=2)
