#!/usr/bin/env python3
"""Summarise raw bench CSVs (stdlib only).

  python3 analysis/summarize.py data/raw/e1/*.csv [--out summary.csv]

Per (exp, controller, target, tag, mode, condition): n, failures, mean, sd,
median, p5/p95/p99, max, bootstrap 95% CIs for mean and median. Warm-up rows
are excluded. For ssh_phases conditions, the median of each phase is printed.
Percentiles use linear interpolation (numpy's default), so p95 is a real
percentile, unlike the legacy harness where it was the maximum.
"""
import argparse
import csv
import json
import math
import random
import statistics
from collections import defaultdict

KEY = ("exp", "controller", "target", "tag", "mode", "condition")
OUT = list(KEY) + ["size", "n", "fails", "mean", "sd", "median", "p5", "p95", "p99", "max",
                   "mean_ci_lo", "mean_ci_hi", "median_ci_lo", "median_ci_hi"]


def pct(xs, q):
    if not xs:
        return math.nan
    k = (len(xs) - 1) * q
    f = math.floor(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def boot(xs, stat, rng, b=2000):
    vals = sorted(stat(rng.choices(xs, k=len(xs))) for _ in range(b))
    return pct(vals, 0.025), pct(vals, 0.975)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    groups, fails, sizes, phases = defaultdict(list), defaultdict(int), {}, defaultdict(lambda: defaultdict(list))
    for path in a.files:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("warmup") == "1":
                    continue
                k = tuple(r[x] for x in KEY)
                sizes[k] = r.get("size", "")
                if r["rc"] != "0":
                    fails[k] += 1
                    continue
                groups[k].append(float(r["dur_ms"]))
                if r.get("phases"):
                    for ph, v in json.loads(r["phases"]).items():
                        phases[k][ph].append(v)

    rows = []
    for k in sorted(set(groups) | set(fails)):
        xs = sorted(groups.get(k, []))
        row = dict(zip(KEY, k), size=sizes[k], n=len(xs), fails=fails[k])
        if xs:
            row.update(mean=statistics.fmean(xs), sd=statistics.stdev(xs) if len(xs) > 1 else 0.0,
                       median=pct(xs, 0.5), p5=pct(xs, 0.05), p95=pct(xs, 0.95), p99=pct(xs, 0.99),
                       max=xs[-1])
            row["mean_ci_lo"], row["mean_ci_hi"] = boot(xs, statistics.fmean, rng)
            row["median_ci_lo"], row["median_ci_hi"] = boot(xs, statistics.median, rng)
        rows.append(row)

    fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else str(v)
    print(f"{'exp':4} {'target':8} {'tag':7} {'mode':6} {'condition':22} {'n':>5} {'fail':>4} "
          f"{'median':>9} {'mean':>9} {'p95':>9} {'p99':>9} {'max':>9}")
    for r in rows:
        print(f"{r['exp']:4} {r['target']:8} {r['tag']:7} {r['mode']:6} {r['condition']:22} "
              f"{r['n']:5d} {r['fails']:4d} " +
              " ".join(f"{fmt(r.get(x, math.nan)):>9}" for x in ("median", "mean", "p95", "p99", "max")))
        k = tuple(r[x] for x in KEY)
        if k in phases:
            print("      phases (median ms from spawn): " +
                  ", ".join(f"{ph}={statistics.median(v):.1f}" for ph, v in phases[k].items()))
    if a.out:
        with open(a.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OUT)
            w.writeheader()
            for r in rows:
                w.writerow({x: fmt(r.get(x, "")) for x in OUT})
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
