#!/usr/bin/env python3
"""Find processes that start periodically (~60 s) on this host. Read-only, stdlib only.

Polls /proc every 0.1 s for --minutes, records every NEW process (time, command line,
parent), then reports commands whose start times repeat with a period between
--pmin and --pmax seconds. Run it on the host directly, or from the controller:

    ssh sshmf-ume-ts 'python3 - --minutes 4' < confirm/find_periodic.py

Processes that live < 0.1 s can be missed; the parent chain usually still shows up.
"""
import argparse
import collections
import os
import statistics
import time


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def info(pid):
    stat = read(f"/proc/{pid}/stat").decode(errors="replace")
    if not stat:
        return None
    comm = stat[stat.find("(") + 1:stat.rfind(")")]
    ppid = int(stat[stat.rfind(")") + 2:].split()[1])
    cmd = read(f"/proc/{pid}/cmdline").replace(b"\0", b" ").decode(errors="replace").strip()
    pcmd = read(f"/proc/{ppid}/cmdline").replace(b"\0", b" ").decode(errors="replace").strip()
    return comm, cmd or f"[{comm}]", ppid, pcmd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=4)
    ap.add_argument("--pmin", type=float, default=50)
    ap.add_argument("--pmax", type=float, default=70)
    a = ap.parse_args()
    t0 = time.time()
    seen = {int(p) for p in os.listdir("/proc") if p.isdigit()}
    starts = collections.defaultdict(list)          # key -> [t]
    example = {}
    print(f"watching new processes for {a.minutes} min on {os.uname().nodename} ...", flush=True)
    while time.time() - t0 < a.minutes * 60:
        now = {int(p) for p in os.listdir("/proc") if p.isdigit()}
        for pid in now - seen:
            i = info(pid)
            if i:
                comm, cmd, ppid, pcmd = i
                key = cmd[:120]
                starts[key].append(time.time() - t0)
                example[key] = f"comm={comm} parent={pcmd[:80]}"
        seen = now
        time.sleep(0.1)

    rows = []
    for key, ts in starts.items():
        # collapse bursts (several starts within 3 s count as one event)
        ev = [ts[0]] + [b for a_, b in zip(ts, ts[1:]) if b - a_ > 3]
        iv = [b - a_ for a_, b in zip(ev, ev[1:])]
        if len(iv) >= 2:
            med = statistics.median(iv)
            if a.pmin <= med <= a.pmax:
                rows.append((med, len(ev), key))
    print(f"\nperiodic candidates (period {a.pmin}-{a.pmax} s):")
    if not rows:
        print("  none found. Try --minutes 6, or run it with sudo to read all command lines.")
    for med, n, key in sorted(rows, key=lambda r: -r[1]):
        print(f"  every {med:5.1f} s  x{n}  {key}\n      {example[key]}")
    print(f"\n{len(starts)} distinct new commands seen in total.")


if __name__ == "__main__":
    main()
