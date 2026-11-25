#!/usr/bin/env python3
"""E8: fan-out.

fleet : N emulated targets (setup/fleet.sh) x concurrency c; tests M3 and M5.
single: c simultaneous calls at ONE sshd (the real target); tests the MaxStartups knee.

Run on the controller:  python3 e8_fanout.py fleet --target ume
                        python3 e8_fanout.py single --target ume
"""
import argparse
import asyncio
import random
import socket
import sys
import time

from sshmf.common import (FMT, RawWriter, dk, load_config, name, out_path,
                          snapshot, write_meta)

CALL_FIELDS = ["run_id", "mode", "N", "c", "round", "idx", "host",
               "start_ms", "dur_ms", "rc", "err_head"]
ROUND_FIELDS = ["run_id", "mode", "N", "c", "round", "t_start_ns", "makespan_ms", "fails"]


async def one(argv, sem, t0, timeout):
    async with sem:
        ts = time.perf_counter()
        p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(p.communicate(), timeout)
            rc = p.returncode
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()
            err, rc = b"TIMEOUT", -9
        te = time.perf_counter()
        return {"start_ms": round((ts - t0) * 1000, 3), "dur_ms": round((te - ts) * 1000, 3),
                "rc": rc, "err_head": err.decode(errors="replace").strip()[:200] if rc else ""}


async def do_round(argvs, c, timeout):
    sem = asyncio.Semaphore(c)
    t0 = time.perf_counter()
    res = await asyncio.gather(*(one(a, sem, t0, timeout) for a in argvs))
    return (time.perf_counter() - t0) * 1000, res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["fleet", "single"])
    ap.add_argument("--target", default="ume")
    ap.add_argument("--Ns", default="10,20,50", help="fleet sizes (fleet mode)")
    ap.add_argument("--cs", default=None, help="concurrency levels")
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--gap", type=float, default=1.0, help="seconds between rounds")
    ap.add_argument("--timeout", type=float, default=60)
    args = ap.parse_args()
    cfg = load_config()
    t = args.target

    if args.mode == "fleet":
        cs = [int(x) for x in (args.cs or "1,5,10,20,50").split(",")]
        grid = [(N, c) for N in map(int, args.Ns.split(",")) for c in cs if c <= N]
    else:
        cs = [int(x) for x in (args.cs or "1,2,5,8,10,12,15,20,30,50").split(",")]
        grid = [(c, c) for c in cs]
    cells = [(N, c, r) for N, c in grid for r in range(args.rounds)]
    random.Random(cfg["run"]["seed"]).shuffle(cells)

    run_id = time.strftime("%Y%m%d-%H%M%S")
    stem = f"{socket.gethostname()}_to_{t}_{args.mode}"
    meta_path = out_path(cfg, "e8", run_id, stem, "meta.json")
    meta = {"run_id": run_id, "argv": sys.argv, "grid": grid, "start": snapshot(cfg, t)}
    write_meta(meta_path, meta)
    calls = RawWriter(out_path(cfg, "e8", run_id, stem + "_calls"), CALL_FIELDS)
    rounds = RawWriter(out_path(cfg, "e8", run_id, stem + "_rounds"), ROUND_FIELDS)
    print(f"E8 {args.mode}: {len(cells)} rounds")

    try:
        for k, (N, c, r) in enumerate(cells):
            if args.mode == "fleet":
                hosts = [f"sshmf-fleet-{i:02d}" for i in range(1, N + 1)]
                argvs = [["docker", "-H", f"ssh://{h}", "version", "--format", FMT] for h in hosts]
            else:
                hosts = [name(t, "ts")] * N
                argvs = [dk(hosts[0], "version", "--format", FMT)] * N
            t_ns = time.time_ns()
            makespan, res = asyncio.run(do_round(argvs, c, args.timeout))
            fails = sum(1 for x in res if x["rc"] != 0)
            for idx, (h, x) in enumerate(zip(hosts, res)):
                calls.write({"run_id": run_id, "mode": args.mode, "N": N, "c": c, "round": r,
                             "idx": idx, "host": h, **x})
            rounds.write({"run_id": run_id, "mode": args.mode, "N": N, "c": c, "round": r,
                          "t_start_ns": t_ns, "makespan_ms": round(makespan, 3), "fails": fails})
            print(f"  {k + 1}/{len(cells)} N={N} c={c} makespan={makespan:.0f} ms fails={fails}",
                  flush=True)
            time.sleep(args.gap)
    except KeyboardInterrupt:
        print("interrupted; rows so far are saved", file=sys.stderr)
    finally:
        calls.close()
        rounds.close()
        meta["end"] = snapshot(cfg, t)
        write_meta(meta_path, meta)


if __name__ == "__main__":
    main()
