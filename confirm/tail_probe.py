#!/usr/bin/env python3
"""Find what on the target coincides with periodic slow SSH logins. Run on kiri:

    python3 confirm/tail_probe.py --target ume --minutes 5

1. Streams sampler.py on the target (per-program CPU ticks, 5 Hz; read-only).
2. Meanwhile makes one `ssh -v <target> true` every 0.5 s and times its phases.
3. Marks slow logins (> 1.5x median), reports their period, and ranks the programs
   whose CPU use rises in the 2 s before/while each slow login, versus normal times.
Raw data: data/raw/tailprobe/<time>_{probe,samples}.csv
"""
import argparse
import collections
import csv
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sshmf.common import ROOT, name, run_phases  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="ume")
    ap.add_argument("--minutes", type=float, default=5)
    a = ap.parse_args()
    alias = name(a.target, "ts")
    dur = a.minutes * 60
    out = ROOT / "data/raw/tailprobe"
    out.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%Y%m%d-%H%M%S") + f"_kiri_to_{a.target}"

    samples = []
    sp = subprocess.Popen(["ssh", alias, f"python3 - --hz 5 --all --duration {dur + 20}"],
                          stdin=open(ROOT / "sampler.py", "rb"), stdout=subprocess.PIPE, text=True)

    def pump():
        rd = csv.DictReader(sp.stdout)
        for r in rd:
            if r["kind"] == "proc":
                samples.append((int(r["t_ns"]), r["key"], int(r["cpu_ticks"])))

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    time.sleep(5)

    probes = []
    print(f"probing {alias} every 0.5 s for {a.minutes} min while sampling its CPU ...", flush=True)
    t_end = time.time() + dur
    while time.time() < t_end:
        r = run_phases(["ssh", "-v", alias, "true"])
        ph = json.loads(r["phases"] or "{}")
        probes.append((r["t_start_ns"], r["t_end_ns"], r["dur_ms"], r["rc"],
                       (ph.get("cmd_sent", 0) - ph.get("session", 0)) if ph else None))
        time.sleep(0.5)
    time.sleep(10)
    sp.terminate()
    th.join(timeout=10)

    with open(out / f"{stem}_probe.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["t_start_ns", "t_end_ns", "dur_ms", "rc", "session_ms"]); w.writerows(probes)
    with open(out / f"{stem}_samples.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["t_ns", "comm", "cpu_ticks"]); w.writerows(samples)

    ok = [p for p in probes if p[3] == 0]
    med = statistics.median(p[2] for p in ok)
    slow = [p for p in ok if p[2] > 1.5 * med]
    print(f"\n{len(ok)} logins, median {med:.0f} ms; slow (>1.5x): {len(slow)}")
    if len(slow) >= 2:
        ts = [p[0] / 1e9 for p in slow]
        ev = [ts[0]] + [b for x, b in zip(ts, ts[1:]) if b - x > 5]
        iv = [b - x for x, b in zip(ev, ev[1:])]
        print("slow-event intervals (s):", " ".join(f"{x:.1f}" for x in iv))
        print("session-setup ms in slow logins:", " ".join(f"{p[4]:.0f}" for p in slow if p[4] is not None))

    # per-program CPU ticks per sample interval
    by = collections.defaultdict(list)
    for t, comm, ticks in samples:
        by[comm].append((t, ticks))
    rate = {}
    for comm, xs in by.items():
        xs.sort()
        rate[comm] = [(t1, (k1 - k0)) for (t0, k0), (t1, k1) in zip(xs, xs[1:]) if k1 >= k0]

    def window_sum(comm, a_ns, b_ns):
        return sum(d for t, d in rate.get(comm, []) if a_ns <= t <= b_ns)

    if not slow:
        print("no slow logins in this window; try --minutes 8"); return
    excess = collections.Counter()
    normal = [p for p in ok if p[2] <= 1.2 * med]
    for comm in rate:
        if comm in ("_all", "_other"):
            continue
        s = statistics.mean(window_sum(comm, p[0] - 2_000_000_000, p[1]) for p in slow)
        n = statistics.mean(window_sum(comm, p[0] - 2_000_000_000, p[1]) for p in normal[:200])
        excess[comm] = s - n
    print("\nprograms using more CPU around slow logins than around normal ones (ticks):")
    for comm, x in excess.most_common(8):
        if x > 0:
            print(f"  {comm:20s} +{x:.1f}")
    print("(_other = programs not in the sampler's watch list; if it dominates, rerun with sudo on the target)")
    print(f"\ndata: {out}/{stem}_*.csv")


if __name__ == "__main__":
    main()
