#!/usr/bin/env python3
"""Time one SSH login per key-exchange algorithm, interleaved and shuffled.
Writes CSV to stdout: round, kex, negotiated, dur_ms, kex_ms (Connection established
-> NEWKEYS received), rc, client_ssh. Rounds < 0 are warm-up."""
import argparse, csv, random, subprocess, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--host", required=True); ap.add_argument("--port", default="2201")
ap.add_argument("--key", required=True); ap.add_argument("--user", default="root")
ap.add_argument("--n", type=int, default=200); ap.add_argument("--warmup", type=int, default=5)
ap.add_argument("--kex", required=True, help="comma-separated KexAlgorithms to compare")
ap.add_argument("--ssh", default="ssh"); ap.add_argument("--seed", type=int, default=1)
a = ap.parse_args()
ver = subprocess.run([a.ssh, "-V"], capture_output=True, text=True).stderr.strip()
kexs = a.kex.split(","); rng = random.Random(a.seed)
w = csv.writer(sys.stdout); w.writerow(["round", "kex", "negotiated", "dur_ms", "kex_ms", "rc", "client_ssh"])
for r in range(-a.warmup, a.n):
    order = kexs[:]; rng.shuffle(order)
    for k in order:
        argv = [a.ssh, "-v", "-p", str(a.port), "-i", a.key, "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "GSSAPIAuthentication=no", "-o", f"KexAlgorithms={k}",
                "-o", "LogLevel=DEBUG1", f"{a.user}@{a.host}", "true"]
        t0 = time.perf_counter(); conn = kexd = None; neg = ""
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for raw in p.stderr:
            now = (time.perf_counter() - t0) * 1000; line = raw.decode(errors="replace")
            if conn is None and "Connection established" in line: conn = now
            if kexd is None and "SSH2_MSG_NEWKEYS received" in line: kexd = now
            if not neg and "kex: algorithm:" in line: neg = line.split("kex: algorithm:")[1].strip()
        rc = p.wait(); dur = (time.perf_counter() - t0) * 1000
        kex_ms = round(kexd - conn, 3) if conn is not None and kexd is not None else ""
        w.writerow([r, k, neg, round(dur, 3), kex_ms, rc, ver]); sys.stdout.flush()
        time.sleep(0.05)
