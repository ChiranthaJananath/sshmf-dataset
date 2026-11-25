#!/usr/bin/env python3
"""Cross-host analysis of the SSH-MF dataset (numpy/pandas/scipy).

    python analysis/analyze_all.py <data_raw_dir> <out_dir>

Writes one CSV per table to <out_dir> and prints the tables. Every number used
in the manuscript should come from this script.
"""
import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

RAW, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
RNG = np.random.default_rng(7)
KEYS = ["connected", "banner", "kex_done", "auth_done", "cmd_sent", "exit_status"]
SEG = ["spawn_tcp", "banner", "kex", "auth", "session", "cmd_exit"]
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


def load(exp, pattern="*.csv"):
    fs = [f for f in glob.glob(str(RAW / exp / pattern))
          if not any(k in f for k in ("samples", "calls", "events", "rounds"))]
    if not fs:
        return pd.DataFrame()
    d = pd.concat([pd.read_csv(f) for f in fs], ignore_index=True)
    return d[d.warmup == 0]


def ci_median(x, b=3000):
    x = np.asarray(x)
    if len(x) < 5:
        return (np.nan, np.nan)
    m = np.median(RNG.choice(x, (b, len(x))), axis=1)
    return tuple(np.percentile(m, [2.5, 97.5]))


def segments(phases):
    p = pd.DataFrame([json.loads(s) for s in phases])
    s = p[KEYS].diff(axis=1)
    s[KEYS[0]] = p[KEYS[0]]
    s.columns = SEG
    return s


def save(df, name, title):
    df.to_csv(OUT / f"{name}.csv")
    print(f"\n=== {title}\n{df.to_string()}")


# ----------------------------------------------------------------------------- E1
e1 = load("e1")
t1, local_ps = pd.DataFrame(), pd.Series(dtype=float)
if len(e1):
    e1["tag"] = e1["tag"].fillna("")
    loc = e1[e1.target == "local"]
    local_ps = loc[loc.condition == "local_cli_ps"].groupby("controller").dur_ms.median()
    local_ps.index = local_ps.index.str.lower()

    rows = []
    for (ctl, tgt, tag), g in e1[e1.target != "local"].groupby(["controller", "target", "tag"]):
        label = f"{tgt}{'(' + tag + ')' if tag else ''}"
        r = {"target": label, "controller": ctl}
        for c in ("ssh_true", "ctx_ps", "tuned_ssh_true"):
            x = g[g.condition == c].dur_ms
            if len(x):
                r[f"{c}_med"] = x.median()
                if c == "ssh_true":
                    r["n"] = len(x)
                    r["ssh_true_lo"], r["ssh_true_hi"] = ci_median(x)
        r["target_local_ps"] = local_ps.get(tgt.lower(), np.nan)
        r["ctx_overhead"] = r.get("ctx_ps_med", np.nan) - r["target_local_ps"]
        for c, pre in (("ssh_phases", ""), ("tuned_ssh_phases", "tuned_")):
            p = g[g.condition == c]
            if len(p):
                s = segments(p.phases).median()
                for k in SEG:
                    r[f"{pre}{k}"] = s[k]
        if "kex" in r and "tuned_kex" in r:
            r["pq_kex_cost"] = r["kex"] - r["tuned_kex"]
        rows.append(r)
    t1 = pd.DataFrame(rows).set_index("target").sort_values("ssh_true_med")
    cols = ["controller", "n", "ssh_true_med", "ssh_true_lo", "ssh_true_hi", "ctx_ps_med", "target_local_ps",
            "ctx_overhead", "spawn_tcp", "banner", "kex", "auth", "session", "cmd_exit", "tuned_kex", "pq_kex_cost"]
    save(t1[[c for c in cols if c in t1]].round(1), "t1_e1_per_target", "E1: login cost and SSH phases per target (ms)")

# ----------------------------------------------------------------------------- E7: alpha vs RTT
e7 = load("e7")
if len(e7):
    e7["rtt"] = e7.tag.str[3:].astype(float)
    rows = []
    for c, g in e7.groupby("condition"):
        m = g.groupby("rtt").dur_ms.median()
        fit = stats.linregress(m.index.values, m.values)
        rows.append({"condition": c, "intercept_ms": fit.intercept, "rtts_per_call": fit.slope, "r2": fit.rvalue ** 2})
    t7 = pd.DataFrame(rows).set_index("condition")
    save(t7.round(4), "t2_e7_rtt_law", "E7: median = a + k * RTT (ume, emulated delay)")
    # out-of-sample check on the real WAN path (Azure)
    rtt = pd.concat([pd.read_csv(f) for f in glob.glob(str(RAW / "azure_rtt" / "*.csv"))]).tcp_connect_ms.median()
    a, k = t7.loc["ssh_true", ["intercept_ms", "rtts_per_call"]]
    if "dci" in t1.index:
        obs = t1.loc["dci", "ssh_true_med"]
        print(f"\nAzure check: RTT {rtt:.1f} ms -> predicted ssh_true {a + k * rtt:.0f} ms, observed {obs:.0f} ms "
              f"({(obs / (a + k * rtt) - 1) * 100:+.1f}%)")

# ----------------------------------------------------------------------------- E2: stats
e2 = load("e2")
if len(e2):
    pairs = {"cli": ("local_cli_stats", "ctx_cli_stats"), "api": ("local_api_stats", "ssh_api_stats"),
             "oneshot": ("local_api_oneshot", "ssh_api_oneshot")}
    rows = []
    for tgt in sorted(set(e2.target) - {"local"}):
        L = e2[(e2.controller.str.lower() == tgt) & (e2.target == "local")]
        R = e2[e2.target == tgt]
        ver = ""
        for k, (lc, rc) in pairs.items():
            for mode in ("closed", "random"):
                lo = L[(L.condition == lc) & (L["mode"] == mode)].set_index("seq").dur_ms
                re = R[(R.condition == rc) & (R["mode"] == mode)].set_index("seq").dur_ms
                j = pd.concat([lo, re], axis=1, keys=["l", "r"]).dropna()
                if len(j):
                    rows.append({"daemon": tgt, "path": k, "mode": mode, "pairs": len(j), "local_med": j.l.median(),
                                 "remote_med": j.r.median(), "delta_med": (j.r - j.l).median(), "delta_mean": (j.r - j.l).mean()})
    t3 = pd.DataFrame(rows).set_index(["daemon", "path", "mode"])
    save(t3.round(1), "t3_e2_stats", "E2: stats latency, remote vs local on the same daemon (paired by schedule)")
    # gap law on the local API, random mode: duration ~ max(1.01 s, 2.0 s - gap)
    rows = []
    for host, g in e2[(e2.target == "local") & (e2.condition == "local_api_stats") & (e2["mode"] == "random")].groupby("controller"):
        pred = np.maximum(1010, 2000 - g.gap_ms)
        err = (g.dur_ms - pred).abs()
        rows.append({"host": host, "n": len(g), "median_abs_err_ms": err.median(), "share_within_60ms": (err < 60).mean()})
    save(pd.DataFrame(rows).set_index("host").round(3), "t4_e2_gap_law", "E2: fit of max(1.01 s, 2.0 s - gap) per daemon host")

# ----------------------------------------------------------------------------- E3: tail
e3 = pd.concat([load("e1"), load("e3")], ignore_index=True)
e3 = e3 if len(e3) else pd.DataFrame(columns=["target", "rc", "tag", "condition", "exp", "dur_ms", "t_start_ns", "phases"])
e3 = e3[(e3.target != "local") & (e3.rc == 0) & (e3.tag.fillna("") == "")
        & e3.condition.isin(["ssh_true", "ssh_phases", "ctx_ps", "ctx_inspect"])]
rows = []
for tgt, g in e3.groupby("target"):
    g = g.copy()
    g["slow"] = g.dur_ms > 1.5 * g.groupby(["exp", "condition"]).dur_ms.transform("median")
    s = g[g.slow & (g.exp == "e3")].sort_values("t_start_ns")
    t = s.t_start_ns.values / 1e9
    ev = [t[0]] + [b for a_, b in zip(t, t[1:]) if b - a_ > 5] if len(t) else []
    iv = np.diff(ev)
    ph = g[g.slow & (g.condition == "ssh_phases")]
    excess = ""
    if len(ph):
        seg = segments(ph.phases) - segments(g[g.condition == "ssh_phases"].phases).median()
        excess = seg.clip(lower=0).sum().idxmax()
    rows.append({"target": tgt, "calls": len(g), "slow_share_pct": g.slow.mean() * 100,
                 "e3_events": len(ev), "median_interval_s": np.median(iv) if len(iv) else np.nan,
                 "share_intervals_60pm1": np.mean(np.abs(iv - 60.4) < 1.0) if len(iv) else np.nan,
                 "excess_phase": excess, "p99_ssh_true": g[g.condition == "ssh_true"].dur_ms.quantile(.99)})
if rows:
    save(pd.DataFrame(rows).set_index("target").round(3), "t5_e3_tail", "E3: slow logins (>1.5x median) per target")

# ----------------------------------------------------------------------------- E4: transport paths
e4 = load("e4")
if len(e4):
    e4 = e4[e4.rc == 0]
    t = e4[~e4.condition.str.startswith("local")].copy()
    t["path"], t["op"] = t.condition.str.split("_", n=1).str[0], t.condition.str.split("_", n=1).str[1]
    p = t[t.op == "ps"].pivot_table(index="target", columns="path", values="dur_ms", aggfunc="median")
    p["target_local_ps"] = [local_ps.get(i.lower(), np.nan) for i in p.index]
    for c in ("ts", "cm", "tls", "tuned"):
        if c in p:
            p[f"{c}_overhead"] = p[c] - p["target_local_ps"]
    save(p.round(1), "t6_e4_paths", "E4: docker ps per path (median ms) and overhead vs the target's local CLI")

# ----------------------------------------------------------------------------- E5: payload
e5 = load("e5")
if len(e5):
    e5 = e5[e5.rc == 0].copy()
    e5["path"] = e5.condition.str.rsplit("_", n=2).str[0]
    e5["MB"] = e5["size"] * 100 / 1e6
    e5["host"] = np.where(e5.target == "local", e5.controller.str.lower(), e5.target)
    rows = []
    for (h, pth), g in e5.groupby(["host", "path"]):
        m = g.groupby("MB").dur_ms.median()
        if len(m) < 3:
            continue
        f = stats.linregress(m.index.values, m.values)
        rows.append({"host": h, "path": pth, "alpha_ms": f.intercept, "ms_per_MB": f.slope,
                     "beta_MBps": 1000 / f.slope, "r2": f.rvalue ** 2, "max_MB": m.index.max()})
    t8 = pd.DataFrame(rows).set_index(["host", "path"])
    eff = []
    for h in t8.index.get_level_values(0).unique():
        if (h, "local") in t8.index:
            for pth in ("ts", "cm"):
                if (h, pth) in t8.index:
                    eff.append({"host": h, "path": pth,
                                "efficiency": t8.loc[(h, "local"), "ms_per_MB"] / t8.loc[(h, pth), "ms_per_MB"]})
    save(t8.round(3), "t7_e5_fits", "E5: T = alpha + P/beta per host and path")
    save(pd.DataFrame(eff).set_index(["host", "path"]).round(3), "t8_e5_efficiency", "E5: bulk efficiency beta/beta_local")

# ----------------------------------------------------------------------------- confirmations
pam = load("pam")
if len(pam):
    pam = pam[pam.rc == 0]
    rows = []
    for c, g in pam.groupby("condition"):
        r = {"condition": c, "n": len(g), "median": g.dur_ms.median(), "p99": g.dur_ms.quantile(.99), "max": g.dur_ms.max()}
        if "phases" in c:
            s = segments(g.phases).median()
            r.update({k: s[k] for k in SEG})
        rows.append(r)
    save(pd.DataFrame(rows).set_index("condition").round(1), "t9_pam", "Clean PAM test on ume (UsePAM yes vs no)")
kx = sorted(glob.glob(str(RAW / "kex" / "*")))
if kx:
    rows = []
    for f in glob.glob(kx[-1] + "/*.csv"):
        d = pd.read_csv(f)
        d = d[(d["round"] >= 0) & (d.rc == 0)]
        for k, g in d.groupby("kex"):
            rows.append({"scenario": Path(f).stem, "kex": k.split("-")[0], "n": len(g),
                         "total_ms": g.dur_ms.median(), "kex_ms": g.kex_ms.median()})
    t10 = pd.DataFrame(rows).pivot_table(index="scenario", columns="kex", values="kex_ms")
    save(t10.round(1), "t10_kex", "Key-exchange microbenchmark: median KEX phase (ms), loopback")

# ----------------------------------------------------------------------------- E9: controller
e9c = sorted(glob.glob(str(RAW / "e9" / "*_cycles.csv")))
if e9c:
    cyc = pd.concat([pd.read_csv(f) for f in e9c], ignore_index=True)
    evf = [f for f in glob.glob(str(RAW / "e9" / "*_events.csv"))]
    ev = pd.concat([pd.read_csv(f) for f in evf], ignore_index=True) if evf else pd.DataFrame()
    metas = [json.load(open(f)) for f in glob.glob(str(RAW / "e9" / "*.meta.json"))]
    def label(m):
        a = m["argv"]
        per = float(a[a.index("--period") + 1]) if "--period" in a else 10.0
        return f"{a[a.index('--variant') + 1]}@{per:g}s"
    cpu = pd.DataFrame([{"variant": label(m), "cpu_s": m.get("controller_cpu_s", np.nan),
                         "cycles": m.get("cycles", np.nan)} for m in metas]).groupby("variant").sum()
    per = {m["run_id"]: label(m) for m in metas}
    cyc["variant"] = cyc.run_id.astype(str).map(per).fillna(cyc.variant)
    if len(ev):
        ev["variant"] = ev.run_id.astype(str).map(per).fillna(ev.variant)
    per_cycle = cyc.drop_duplicates(["run_id", "variant", "block", "cycle"])
    rows = []
    order = sorted(cyc.variant.unique(), key=lambda x: (["naive", "reuse", "batch", "v2"].index(x.split("@")[0]), x))
    for v in order:
        c = per_cycle[per_cycle.variant == v]
        h = cyc[cyc.variant == v]
        if not len(c):
            continue
        r = {"variant": v, "cycles": len(c), "makespan_med_ms": c.makespan_ms.median(),
             "makespan_p95_ms": c.makespan_ms.quantile(.95),
             "overrun_pct": (c.makespan_ms > c.period_s * 1000).mean() * 100 if "period_s" in c else np.nan,
             "host_ms_med": h.host_ms.median(), "failed_host_polls_pct": (1 - h.ok.mean()) * 100,
             "timeouts": h.timeouts.sum(), "retries": h.retries.sum(),
             "new_ssh_sessions_per_cycle": h.groupby(["run_id", "cycle"]).new_ssh_sessions.sum().mean(),
             "controller_cpu_ms_per_cycle": cpu.loc[v, "cpu_s"] / cpu.loc[v, "cycles"] * 1000 if v in cpu.index else np.nan}
        if len(ev):
            e = ev[ev.variant == v]
            if len(e):
                det = (e.t_detect_ns - e.t_inject_ns) / 1e9
                rep = (e.t_repair_ns - e.t_inject_ns) / 1e9
                r.update({"faults": len(e), "detect_med_s": det.median(), "detect_p95_s": det.quantile(.95),
                          "repair_med_s": rep.median(), "repair_p95_s": rep.quantile(.95)})
        rows.append(r)
    t11 = pd.DataFrame(rows).set_index("variant")
    save(t11.round(2), "t11_e9_controller", "E9: supervision loop over 4 hosts, by controller variant")
    if len(ev):
        det = {v: (g.t_detect_ns - g.t_inject_ns) / 1e9 for v, g in ev.groupby("variant")}
        for a_, b_ in (("naive@10s", "v2@10s"), ("naive@10s", "v2@1s")):
            if a_ in det and b_ in det and len(det[a_]) > 3 and len(det[b_]) > 3:
                u = stats.mannwhitneyu(det[a_], det[b_])
                print(f"E9 detection latency {a_} vs {b_}: medians {det[a_].median():.2f} vs {det[b_].median():.2f} s, "
                      f"Mann-Whitney p = {u.pvalue:.2e}")
    busy = cyc[cyc.cpu_pct.notna() & cyc.block.astype(str).str.startswith("fast")]
    if len(busy):
        save(busy.groupby("variant").cpu_pct.describe()[["count", "mean", "50%", "std"]].round(2),
             "t12_e9_cpu_accuracy", "E9 fast: CPU% of the 25%-capped busy container, CLI (naive) vs controller-side (v2)")
