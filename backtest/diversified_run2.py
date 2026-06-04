"""Second diversified pass (~55 fresh names) with an IMPROVED classifier.

Tests whether the two fixes that mattered last time actually improve signal
performance: (1) normalized P/E (median EPS, not a single distorted year), and
(2) cyclical-vs-structural separation of loss-making names. Point-in-time via
SEC EDGAR (full coverage). Compares forward returns by bucket.
"""
from __future__ import annotations
import json, statistics, sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pit import snapshot, forward_return, annual_history_as_of

FIELDS = {
    "Internet": ["UBER", "ABNB", "SHOP", "PYPL", "SNAP", "SPOT"],
    "Semis": ["QCOM", "TXN", "AMAT", "LRCX", "KLAC", "ON"],
    "Software": ["PLTR", "SNOW", "NET", "DDOG", "PANW", "CRWD"],
    "Pharma/Bio": ["ABBV", "BMY", "GILD", "AMGN", "MRNA", "VRTX"],
    "Staples": ["PEP", "MDLZ", "CL", "KMB", "EL"],
    "Retail/Disc": ["HD", "LOW", "MCD", "CMG", "LULU", "RH"],
    "Industrials": ["RTX", "NOC", "GD", "UPS", "FDX", "MMM"],
    "Energy": ["VLO", "MPC", "KMI", "DVN", "FANG"],
    "Fin/Pmts": ["V", "MA", "AXP", "BLK"],
    "EV": ["RIVN", "LCID"],
    "Media/Tel": ["TMUS", "WBD", "ROKU"],
    "Banks": ["MS", "COF"],          # MODEL-N/A
    "REIT": ["AMT", "EQIX", "PSA"],  # MODEL-N/A
}
MODEL_NA = {"Banks", "REIT"}
DATES = [date(2021, 6, 15), date(2022, 12, 15)]


def classify(snap, hist, model_na):
    if model_na:
        return "MODEL-N/A"
    f, v = snap["fundamentals"], snap["valuation"]
    price = snap["price_metrics"].get("price")
    fcf, neg_eq = f.get("free_cash_flow"), f.get("negative_equity")
    rev_latest = f.get("revenue")

    eps_vals = [h["eps"] for h in hist if h["eps"] is not None]
    op_vals = [h["op_income"] for h in hist if h["op_income"] is not None]   # chrono
    rev_vals = [h["revenue"] for h in hist if h["revenue"] is not None]
    med_eps = statistics.median(eps_vals) if eps_vals else None
    norm_pe = (price / med_eps) if (price and med_eps and med_eps > 0) else None

    currently_loss = bool(op_vals) and op_vals[-1] < 0
    profitable_prior = sum(1 for o in op_vals[:-1] if o > 0)
    # rev_vals is chronological (oldest->newest); declining = each year below prior
    rev_declining = (len(rev_vals) >= 3
                     and all(rev_vals[i] < rev_vals[i - 1] for i in range(1, len(rev_vals))))

    if neg_eq:
        return "DISTRESS-STRUCT"
    if currently_loss:
        if rev_declining and profitable_prior <= 1:
            return "DISTRESS-STRUCT"
        if profitable_prior >= 2:
            return "CYCLICAL-TROUGH"
        return "DISTRESS-STRUCT"
    if (med_eps is None or med_eps <= 0) and (rev_latest is None or rev_latest < 100_000_000):
        return "SPECULATIVE"
    if norm_pe is not None:
        if norm_pe < 18 and (fcf is None or fcf > 0):
            return "CHEAP-QUALITY(norm)"
        if norm_pe > 40:
            return "EXPENSIVE"
        return "FAIR"
    return "FAIR"


def main():
    rows = []
    for field, tickers in FIELDS.items():
        na = field in MODEL_NA
        for t in tickers:
            for d in DATES:
                rec = {"field": field, "ticker": t, "date": d.isoformat(), "model_na": na}
                try:
                    s = snapshot(t, d)
                    hist = [] if na else annual_history_as_of(t, d, 5)
                    rec.update({
                        "price": s["price_metrics"].get("price"),
                        "bucket": classify(s, hist, na),
                        "fwd_1y": forward_return(t, d, 365),
                        "fwd_2y": forward_return(t, d, 730),
                        "fwd_3y": forward_return(t, d, 1095),
                    })
                except Exception as e:
                    rec["bucket"] = "ERROR"; rec["error"] = str(e)[:70]
                rows.append(rec)
    out = Path(__file__).resolve().parent / "_cache" / "diversified_results2.json"
    out.write_text(json.dumps(rows, indent=1))

    def agg(sub, k):
        vals = [r[k] for r in sub if r.get(k) is not None]
        return (round(statistics.mean(vals), 1), round(statistics.median(vals), 1), len(vals)) if vals else (None, None, 0)

    errs = [r for r in rows if r["bucket"] == "ERROR"]
    print(f"cells: {len(rows)} | errors/skips: {len(errs)}")
    print("\n=== forward return by IMPROVED bucket ===")
    print(f"{'bucket':<20}{'n':>4}{'mean1y':>9}{'med1y':>8}{'mean2y':>9}{'med2y':>8}{'mean3y':>9}")
    for b in ["CHEAP-QUALITY(norm)", "FAIR", "EXPENSIVE", "CYCLICAL-TROUGH",
              "SPECULATIVE", "DISTRESS-STRUCT", "MODEL-N/A"]:
        sub = [r for r in rows if r.get("bucket") == b]
        if not sub:
            continue
        m1, m2, m3 = agg(sub, "fwd_1y"), agg(sub, "fwd_2y"), agg(sub, "fwd_3y")
        print(f"{b:<20}{len(sub):>4}{str(m1[0]):>9}{str(m1[1]):>8}{str(m2[0]):>9}{str(m2[1]):>8}{str(m3[0]):>9}")

    print("\n=== KEY COMPARISONS ===")
    cyc = agg([r for r in rows if r["bucket"] == "CYCLICAL-TROUGH"], "fwd_2y")
    strc = agg([r for r in rows if r["bucket"] == "DISTRESS-STRUCT"], "fwd_2y")
    print(f"CYCLICAL-TROUGH 2y mean={cyc[0]} (n={cyc[2]}) vs DISTRESS-STRUCT 2y mean={strc[0]} (n={strc[2]})")
    chq = agg([r for r in rows if r["bucket"] == "CHEAP-QUALITY(norm)"], "fwd_2y")
    print(f"CHEAP-QUALITY(normalized) 2y mean={chq[0]} median={chq[1]} (n={chq[2]})  [prior crude run: mean +15 / median +7]")

    print("\n=== by DATE x bucket (2y mean) ===")
    for d in DATES:
        ds = d.isoformat(); line = ds + ": "
        for b in ["CHEAP-QUALITY(norm)", "FAIR", "EXPENSIVE", "CYCLICAL-TROUGH", "DISTRESS-STRUCT"]:
            sub = [r for r in rows if r["date"] == ds and r["bucket"] == b]
            line += f"{b.split('(')[0]}={agg(sub,'fwd_2y')[0]}({len(sub)})  "
        print(line)

    if errs:
        print("\nskips:", ", ".join(f"{r['ticker']}@{r['date'][:7]}" for r in errs))


if __name__ == "__main__":
    main()
