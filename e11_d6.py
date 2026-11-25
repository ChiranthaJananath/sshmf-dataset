#!/usr/bin/env python3
"""E11: what design rule D6 frees and what it costs, on the emulated fleet of E10.

D6 closes a host's persistent SSH session after an idle period and reopens it when polling
resumes. Two conditions alternate in pairs (keep, d6, d6, keep, keep, d6 by default):
  keep : sessions stay open through the idle phase (ControlPersist keeps them)
  d6   : sessions are closed RELEASE_AFTER s into the idle phase
Each pair: prepare (keep: open all sessions; d6: close them) -> POLL s of v2 polling at
PERIOD s -> IDLE s without polling -> one measured "resume" cycle (the price of D6).
Samplers record memory on the target (sshd of the emulated hosts) and on the controller.

    python3 e11_d6.py --n 100 --dry-run      # print the plan and its duration
    python3 e11_d6.py --n 100                # ~70 min with the defaults
    SSHMF_FAKE=1 python3 e11_d6.py --n 5 --poll 6 --idle 6 --release-after 2 --pairs 2   # logic test
"""
import argparse
import socket
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import e9_controller as e9
from sshmf.common import ROOT, RawWriter, load_config, name, out_path, snapshot, write_meta

ORDER = ["keep", "d6", "d6", "keep", "keep", "d6"]
CYCLE_FIELDS = ["run_id", "pair", "condition", "phase", "cycle", "t_start_ns", "makespan_ms",
                "polls", "ok", "timeouts"]
EVENT_FIELDS = ["t_ns", "event", "pair", "condition", "detail"]


def start_sampler(argv, path, stdin=None):
    """Run sampler.py (locally or over SSH); stamp every line with the controller receive time."""
    f = open(path, "w")
    p = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE, text=True)

    def pump():
        first = True
        for line in p.stdout:
            f.write(line.rstrip("\n") + (",ctl_recv_ns\n" if first else f",{time.time_ns()}\n"))
            first = False
            f.flush()
        f.close()

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    return p, th


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=100, help="emulated hosts ex001..ex<n> (see e10_setup.py)")
    ap.add_argument("--target", default="ume", help="real host that runs the emulated fleet")
    ap.add_argument("--pairs", type=int, default=6)
    ap.add_argument("--poll", type=float, default=300, help="seconds of polling per pair")
    ap.add_argument("--idle", type=float, default=300, help="seconds without polling per pair")
    ap.add_argument("--release-after", type=float, default=60, help="D6 idle timeout, seconds")
    ap.add_argument("--period", type=float, default=1.0)
    ap.add_argument("--hz", type=float, default=1.0, help="sampler rate")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    hosts = [f"ex{i:03d}" for i in range(1, a.n + 1)]
    plan = [ORDER[i % len(ORDER)] for i in range(a.pairs)]
    prep = 70.0
    total = a.pairs * (prep + a.poll + a.idle + 10) + 60
    print(f"E11 (D6): {a.n} hosts, {a.pairs} pairs {plan}, poll {a.poll:.0f} s, idle {a.idle:.0f} s, "
          f"release after {a.release_after:.0f} s; ~{total / 60:.0f} min")
    if a.dry_run:
        return
    if e9.FAKE:
        e9.FAKE_STATE.update({h: True for h in hosts})

    run_id = time.strftime("%Y%m%d-%H%M%S")
    stem = f"{socket.gethostname()}_d6_n{a.n}"
    meta_path = out_path(cfg, "e11", run_id, stem, "meta.json")
    meta = {"run_id": run_id, "argv": sys.argv, "hosts": a.n, "plan": plan, "fake": e9.FAKE,
            "poll_s": a.poll, "idle_s": a.idle, "release_after_s": a.release_after, "period_s": a.period}
    if not e9.FAKE:
        meta["start"] = snapshot(cfg, a.target)
    write_meta(meta_path, meta)
    cyc = RawWriter(out_path(cfg, "e11", run_id, stem + "_cycles"), CYCLE_FIELDS)
    ev = RawWriter(out_path(cfg, "e11", run_id, stem + "_events"), EVENT_FIELDS)

    def event(e, pair="", cond="", detail=""):
        ev.write({"t_ns": time.time_ns(), "event": e, "pair": pair, "condition": cond, "detail": detail})
        print(f"  [{time.strftime('%H:%M:%S')}] {e} {pair} {cond} {detail}", flush=True)

    tr = e9.V2(hosts)
    pool = ThreadPoolExecutor(max_workers=len(hosts))

    def cycle(pair, cond, phase, k):
        t_ns, t0 = time.time_ns(), time.perf_counter()
        res = list(pool.map(lambda h: tr.collect(h, False), hosts))
        span = round((time.perf_counter() - t0) * 1000, 3)
        cyc.write({"run_id": run_id, "pair": pair, "condition": cond, "phase": phase, "cycle": k,
                   "t_start_ns": t_ns, "makespan_ms": span, "polls": len(res),
                   "ok": sum(r["ok"] for r in res), "timeouts": sum(r["timeouts"] for r in res)})
        return span

    def close_all():
        t0 = time.perf_counter()
        list(pool.map(lambda h: e9.sh(["ssh", "-O", "exit", name(h, "cm")], 10), hosts))
        return round((time.perf_counter() - t0) * 1000, 1)

    samplers = []
    if not e9.FAKE:
        smp = ROOT / "sampler.py"
        samplers.append(start_sampler(
            ["ssh", name(a.target, "ts"), f"python3 - --hz {a.hz} --duration {total}"],
            out_path(cfg, "e11", run_id, f"{stem}_samples_{a.target}"), stdin=open(smp, "rb")))
        samplers.append(start_sampler(
            [sys.executable, str(smp), "--hz", str(a.hz), "--duration", str(total), "--all"],
            out_path(cfg, "e11", run_id, f"{stem}_samples_{socket.gethostname()}")))
        time.sleep(10)
    event("samplers_started")

    try:
        for pair, cond in enumerate(plan, start=1):
            event("prepare", pair, cond, f"closed in {close_all()} ms")
            if cond == "keep":
                event("opened", pair, cond, f"{cycle(pair, cond, 'prepare', 0):.0f} ms")
            time.sleep(prep - 10 if not e9.FAKE else 1)   # let memory settle before polling
            event("poll_start", pair, cond)
            end, nxt, k = time.monotonic() + a.poll, time.monotonic(), 0
            while time.monotonic() < end:
                cycle(pair, cond, "poll", k)
                k += 1
                nxt += a.period
                time.sleep(max(0.0, nxt - time.monotonic()))
            event("idle_start", pair, cond, f"{k} cycles")
            if cond == "d6":
                time.sleep(a.release_after)
                event("released", pair, cond, f"closed in {close_all()} ms")
                time.sleep(max(0.0, a.idle - a.release_after))
            else:
                time.sleep(a.idle)
            event("resume", pair, cond, f"{cycle(pair, cond, 'resume', 0):.0f} ms")
            for j in range(1, 6):   # the cycles that follow the resume, for comparison
                cycle(pair, cond, "after_resume", j)
                time.sleep(a.period)
            event("pair_end", pair, cond)
    except KeyboardInterrupt:
        meta["status"] = "interrupted"
        event("interrupted")
    finally:
        event("closing", detail=f"closed in {close_all()} ms")
        for p, th in samplers:
            p.terminate()
            th.join(timeout=10)
        pool.shutdown(wait=False)
        cyc.close()
        ev.close()
        meta.setdefault("status", "complete")
        if not e9.FAKE:
            meta["end"] = snapshot(cfg, a.target)
        write_meta(meta_path, meta)

    bundle = ROOT / "results" / f"sshmf_e11_{run_id}.tar.gz"
    bundle.parent.mkdir(exist_ok=True)
    with tarfile.open(bundle, "w:gz") as tf:
        for f in sorted(Path(meta_path).parent.glob(f"{run_id}_*")):
            tf.add(f, arcname=f"e11/{f.name}")
    print(f"done ({meta['status']}). Send me: {bundle.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
