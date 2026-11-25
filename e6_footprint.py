#!/usr/bin/env python3
"""E6: target footprint vs duty cycle, plus resident-agent baselines.

Run on the controller:  python3 e6_footprint.py --target ume
Samplers run on both hosts for the whole experiment; phase boundaries are
written to an events file using the controller clock, and every remote
sample line is also stamped with the controller receive time (ctl_recv_ns).

Per (variant, duty): idle_pre -> burst at duty d -> idle_post.
Variant 'cm' keeps an SSH master open through idle_post (the resident cost
of session reuse), then closes it with `ssh -O exit`.
Agent block: portainer/agent runs for one burst-length window.
node_exporter already runs on both hosts and is sampled throughout.
"""
import argparse
import random
import socket
import subprocess
import sys
import threading
import time

from sshmf.common import (ROOT, RawWriter, dk, load_config, name, out_path,
                          run_timed, snapshot, write_meta)

AGENT = "sshmf-agent"


def start_remote_sampler(alias, duration, hz, path):
    f = open(path, "w")
    p = subprocess.Popen(["ssh", alias, f"python3 - --hz {hz} --duration {duration}"],
                         stdin=open(ROOT / "sampler.py", "rb"),
                         stdout=subprocess.PIPE, text=True)

    def pump():
        first = True
        for line in p.stdout:
            line = line.rstrip("\n")
            f.write(line + (",ctl_recv_ns\n" if first else f",{time.time_ns()}\n"))
            first = False
            f.flush()
        f.close()

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    return p, th


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="ume")
    ap.add_argument("--duties", default="0.01,0.05,0.2,0.5,1.0")
    ap.add_argument("--variants", default="ts,cm")
    ap.add_argument("--pre", type=float, default=60)
    ap.add_argument("--burst", type=float, default=180)
    ap.add_argument("--post", type=float, default=120)
    ap.add_argument("--hz", type=float, default=4)
    ap.add_argument("--no-agent", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    t = args.target
    ts = name(t, "ts")

    plan = [(v, float(d)) for v in args.variants.split(",") for d in args.duties.split(",")]
    random.Random(cfg["run"]["seed"]).shuffle(plan)
    block = args.pre + args.burst + args.post
    total = len(plan) * block + (0 if args.no_agent else block) + 60
    print(f"E6: {len(plan)} duty blocks + {'no ' if args.no_agent else ''}agent block, "
          f"~{total / 60:.0f} min")
    if args.dry_run:
        for v, d in plan:
            print(f"  variant={v} duty={d}")
        return

    run_id = time.strftime("%Y%m%d-%H%M%S")
    host = socket.gethostname()
    stem = f"{host}_to_{t}"
    meta_path = out_path(cfg, "e6", run_id, stem, "meta.json")
    meta = {"run_id": run_id, "argv": sys.argv, "plan": plan, "start": snapshot(cfg, t)}
    write_meta(meta_path, meta)

    ev = RawWriter(out_path(cfg, "e6", run_id, stem + "_events"),
                   ["t_ns", "event", "variant", "duty"])
    calls = RawWriter(out_path(cfg, "e6", run_id, stem + "_calls"),
                      ["t_start_ns", "t_end_ns", "dur_ms", "rc", "variant", "duty", "err_head"])

    def event(e, v="", d=""):
        ev.write({"t_ns": time.time_ns(), "event": e, "variant": v, "duty": d})
        print(f"  [{time.strftime('%H:%M:%S')}] {e} {v} {d}", flush=True)

    subprocess.run(["ssh", "-O", "exit", name(t, "cm")], capture_output=True)
    subprocess.run(dk(ts, "rm", "-f", AGENT), capture_output=True)

    local = subprocess.Popen([sys.executable, str(ROOT / "sampler.py"), "--hz", str(args.hz),
                              "--duration", str(total),
                              "--out", str(out_path(cfg, "e6", run_id, f"{stem}_samples_{host}"))])
    remote, pump = start_remote_sampler(ts, total, args.hz,
                                        out_path(cfg, "e6", run_id, f"{stem}_samples_{t}"))
    time.sleep(10)
    event("samplers_started")

    try:
        for v, d in plan:
            ctx = name(t, v)
            event("idle_pre", v, d)
            time.sleep(args.pre)
            event("burst_start", v, d)
            end = time.time() + args.burst
            while time.time() < end:
                r = run_timed(dk(ctx, "ps"))
                calls.write({**r, "variant": v, "duty": d})
                rest = r["dur_ms"] / 1000 * (1 - d) / d
                time.sleep(max(0.0, min(rest, end - time.time())))
            event("burst_end", v, d)
            time.sleep(args.post)
            if v == "cm":
                subprocess.run(["ssh", "-O", "exit", ctx], capture_output=True)
                event("cm_master_closed", v, d)
        if not args.no_agent:
            event("agent_idle_pre")
            time.sleep(args.pre)
            subprocess.run(dk(ts, "run", "-d", "--name", AGENT,
                              "-v", "/var/run/docker.sock:/var/run/docker.sock",
                              "-v", "/var/lib/docker/volumes:/var/lib/docker/volumes",
                              "-p", "<redacted-ip>:19001:9001", "portainer/agent:latest"),
                           check=True, capture_output=True)
            event("agent_start")
            time.sleep(args.burst)
            subprocess.run(dk(ts, "rm", "-f", AGENT), capture_output=True)
            event("agent_stop")
            time.sleep(args.post)
            event("agent_idle_post_end")
    except KeyboardInterrupt:
        event("interrupted")
    finally:
        subprocess.run(dk(ts, "rm", "-f", AGENT), capture_output=True)
        local.terminate()
        remote.terminate()
        pump.join(timeout=10)
        ev.close()
        calls.close()
        meta["end"] = snapshot(cfg, t)
        write_meta(meta_path, meta)


if __name__ == "__main__":
    main()
