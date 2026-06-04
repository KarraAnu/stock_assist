"""Systematic diversified backtest: ~55 names x 2 anchor dates.

Applies a transparent mechanical signal rule to each point-in-time snapshot,
then measures whether forward returns sort by signal bucket. Mechanical (not
hand-judged) to minimize hindsight bias.
"""
from __future__ import annotations
import json, sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pit import snapshot, forward_return

FIELDS = {
    "BigTech": ["AAPL", "MSFT", "GOOGL", "AMZN"],
    "Semis": ["NVDA", "AMD", "INTC", "MU", "AVGO"],
    "Banks": ["JPM", "BAC", "GS", "SCHW", "C"],
    "Pharma/Health": ["PFE", "JNJ", "MRK", "LLY", "UNH"],
    "Energy": ["XOM", "CVX", "OXY", "COP", "SLB"],
    "Staples": ["COST", "WMT", "PG", "KO", "MO"],
    "Discretionary": ["TSLA", "F", "GM", "NKE", "SBUX"],
    "Industrials": ["BA", "CAT", "GE", "LMT", "HON"],
    "Travel": ["DAL", "UAL", "CCL", "MAR"],
    "Media/Comm": ["DIS", "NFLX", "T", "VZ"],
    "Software": ["CRM", "ORCL", "ADBE", "NOW"],
    "REIT": ["O", "PLD", "SPG"],
}
MODEL_NA_FIELDS = {"Banks", "REIT"}  # ratios/FCF/Altman don't apply
DATES = [date(2021, 6, 15), date(2022, 12, 15)]


def classify(snap: dict, model_na: bool) -> str:
    if model_na:
        return "MODEL-N/A"
    f = snap["fundamentals"]; v = snap["valuation"]
    ni = f.get("net_income"); eq = f.get("equity"); fcf = f.get("free_cash_flow")
    pe = v.get("pe"); ps = v.get("ps")
    if f.get("negative_equity") or (fcf is not None and fcf < 0 and ni is not None and ni < 0):
        return "DISTRESS"
    if pe is None or pe <= 0:   # no positive trailing earnings
        return "SPECULATIVE"
    roe = (ni / eq) if (ni is not None and eq and eq > 0) else None
    if pe < 18 and (fcf is None or fcf > 0) and (roe is None or roe > 0.10):
        return "CHEAP-QUALITY"
    if pe > 40 or (ps is not None and ps > 12):
        return "EXPENSIVE"
    return "FAIR"


def main():
    rows = []
    for field, tickers in FIELDS.items():
        na = field in MODEL_NA_FIELDS
        for t in tickers:
            for d in DATES:
                rec = {"field": field, "ticker": t, "date": d.isoformat(),
                       "model_na": na}
                try:
                    s = snapshot(t, d)
                    f, v, p = s["fundamentals"], s["valuation"], s["price_metrics"]
                    rec.update({
                        "price": p.get("price"), "pe": v.get("pe"), "ps": v.get("ps"),
                        "fcf": f.get("free_cash_flow"), "ni": f.get("net_income"),
                        "neg_eq": f.get("negative_equity"),
                        "bucket": classify(s, na),
                        "fwd_1y": forward_return(t, d, 365),
                        "fwd_2y": forward_return(t, d, 730),
                        "fwd_3y": forward_return(t, d, 1095),
                    })
                except Exception as e:
                    rec["error"] = str(e)[:80]
                    rec["bucket"] = "ERROR"
                rows.append(rec)
    out = Path(__file__).resolve().parent / "_cache" / "diversified_results.json"
    out.write_text(json.dumps(rows, indent=1))

    # ---- digest ----
    import statistics
    def agg(subset, key="fwd_2y"):
        vals = [r[key] for r in subset if r.get(key) is not None]
        return (round(statistics.mean(vals), 1), round(statistics.median(vals), 1),
                len(vals)) if vals else (None, None, 0)

    print(f"total cells: {len(rows)} | errors: {sum(1 for r in rows if r['bucket']=='ERROR')}")
    print("\n=== forward return by BUCKET (mechanical signal) ===")
    print(f"{'bucket':<14}{'n':>4}{'mean1y':>9}{'med1y':>8}{'mean2y':>9}{'med2y':>8}{'mean3y':>9}")
    for b in ["CHEAP-QUALITY", "FAIR", "EXPENSIVE", "SPECULATIVE", "DISTRESS", "MODEL-N/A"]:
        sub = [r for r in rows if r.get("bucket") == b]
        if not sub: continue
        m1 = agg(sub, "fwd_1y"); m2 = agg(sub, "fwd_2y"); m3 = agg(sub, "fwd_3y")
        print(f"{b:<14}{len(sub):>4}{str(m1[0]):>9}{str(m1[1]):>8}{str(m2[0]):>9}{str(m2[1]):>8}{str(m3[0]):>9}")

    print("\n=== by DATE x bucket (2y fwd mean) ===")
    for d in DATES:
        ds = d.isoformat()
        line = f"{ds}: "
        for b in ["CHEAP-QUALITY", "FAIR", "EXPENSIVE", "SPECULATIVE", "DISTRESS"]:
            sub = [r for r in rows if r["date"] == ds and r.get("bucket") == b]
            mean = agg(sub, "fwd_2y")[0]
            line += f"{b}={mean}({len(sub)})  "
        print(line)

    print("\n=== notable cells ===")
    valid = [r for r in rows if r.get("fwd_2y") is not None and not r["model_na"]]
    def fmt(r):
        return "{}@{}[{}]={}%".format(r["ticker"], r["date"][:7], r["bucket"], r["fwd_2y"])
    for label, rev in [("best fwd_2y", True), ("worst fwd_2y", False)]:
        top = sorted(valid, key=lambda r: r["fwd_2y"], reverse=rev)[:6]
        print(" " + label + ": " + ", ".join(fmt(r) for r in top))

    print("\n=== errors/skips ===")
    for r in rows:
        if r["bucket"] == "ERROR":
            print(f"  {r['ticker']}@{r['date']}: {r.get('error')}")


if __name__ == "__main__":
    main()
