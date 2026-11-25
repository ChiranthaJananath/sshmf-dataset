#!/usr/bin/env python3
"""SSH-MF benchmark runner for E1-E5 and E7.

Run on the controller:      python3 bench.py e1 --target ume
Local baseline, on target:  python3 bench.py e1 --local-only
Every call is written as one raw row; nothing is aggregated here.
"""
import argparse
import random
import shutil
import socket
import subprocess
import sys
import time

from sshmf.common import (FMT, RawWriter, dk, load1, load_config, name, observe_only,
                          out_path, probe_of, run_phases, run_timed, snapshot, write_meta)

SOCK = "/var/run/docker.sock"
LOG_SIZES = [50, 200, 1000, 5000, 10000, 50000, 100000, 500000]


class C:
    def __init__(self, name, argv, *, local=False, needs_container=False,
                 n=None, phases=False, size=""):
        self.name, self.argv, self.local = name, argv, local
        self.needs_container, self.n, self.phases, self.size = needs_container, n, phases, size


def curl_local(path):
    return ["curl", "-sS", "--unix-socket", SOCK, f"http://localhost{path}"]


def curl_remote(path):
    return f"curl -sS --unix-socket {SOCK} 'http://localhost{path}'"


def ssh_ok(alias):
    try:
        return subprocess.run(["ssh", "-o", "ConnectTimeout=5", alias, "true"],
                              capture_output=True, timeout=20).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def context_ok(context):
    """True if the context answers. SSH contexts (alias == context name) get a fast
    5 s reachability probe first, so an unreachable path costs seconds, not minutes."""
    try:
        if not context.endswith("-tls") and not ssh_ok(context):
            raise RuntimeError
        ok = subprocess.run(dk(context, "version", "--format", FMT),
                            capture_output=True, timeout=90).returncode == 0
    except (subprocess.TimeoutExpired, RuntimeError):
        ok = False
    if not ok:
        print(f"note: context {context} unreachable, its conditions are skipped", file=sys.stderr)
    return ok


def remote_curl(t):
    p = subprocess.run(["ssh", name(t, "ts"), "command -v curl"], capture_output=True, timeout=60)
    if p.returncode != 0:
        print(f"note: no curl on {t}, raw-API conditions are skipped", file=sys.stderr)
    return p.returncode == 0


def logs_n(lines):
    return 50 if lines <= 10000 else 30 if lines <= 100000 else 20


# ---------------------------------------------------------------- experiments
def e1(t, cfg):
    """Decomposition of alpha: each condition adds one layer to the previous."""
    ts, probe = name(t, "ts"), probe_of(cfg, t)
    conds = [
        C("local_cli_version", dk("default", "version", "--format", FMT), local=True),
        C("local_cli_ps", dk("default", "ps"), local=True),
        C("local_cli_inspect", dk("default", "inspect", probe), local=True, needs_container=True),
        C("local_api_ps", curl_local("/containers/json"), local=True),
        C("local_api_inspect", curl_local(f"/containers/{probe}/json"), local=True, needs_container=True),
        C("ssh_true", ["ssh", ts, "true"]),
        C("ssh_phases", ["ssh", "-v", ts, "true"], phases=True),
        C("ssh_api_ps", ["ssh", ts, curl_remote("/containers/json")]),
        C("ssh_cli_version", ["ssh", ts, f"docker --context default version --format '{FMT}'"]),
        C("ctx_version", dk(ts, "version", "--format", FMT)),
        C("ctx_ps", dk(ts, "ps")),
        C("ctx_inspect", dk(ts, "inspect", probe)),
    ]
    if not remote_curl(t):
        conds = [c for c in conds if c.name != "ssh_api_ps"]
    tuned = name(t, "tuned")
    if context_ok(tuned):   # same path, client-side SSH tuning only
        conds += [C("tuned_ssh_true", ["ssh", tuned, "true"]),
                  C("tuned_ssh_phases", ["ssh", "-v", tuned, "true"], phases=True),
                  C("tuned_ctx_ps", dk(tuned, "ps"))]
    fleet = "sshmf-fleet-01"
    if not observe_only(cfg, t) and ssh_ok(fleet):   # minimal sshd without PAM, same host
        conds += [C("fleet_ssh_true", ["ssh", fleet, "true"]),
                  C("fleet_ssh_phases", ["ssh", "-v", fleet, "true"], phases=True)]
    return conds


def e2(t, cfg):
    """Phase absorption: CLI stats vs raw API stream=false vs one-shot."""
    ts, probe = name(t, "ts"), probe_of(cfg, t)
    q = f"/containers/{probe}/stats?stream=false"
    conds = [
        C("local_cli_stats", dk("default", "stats", "--no-stream", probe), local=True, needs_container=True),
        C("local_api_stats", curl_local(q), local=True, needs_container=True),
        C("local_api_oneshot", curl_local(q + "&one-shot=true"), local=True, needs_container=True),
        C("ctx_cli_stats", dk(ts, "stats", "--no-stream", probe)),
        C("ssh_api_stats", ["ssh", ts, curl_remote(q)]),
        C("ssh_api_oneshot", ["ssh", ts, curl_remote(q + "&one-shot=true")]),
    ]
    return conds if remote_curl(t) else [c for c in conds if not c.name.startswith("ssh_api")]


def e3(t, cfg):
    """Tail shape: large N of short calls, with SSH phase split."""
    ts, probe = name(t, "ts"), probe_of(cfg, t)
    return [
        C("ssh_true", ["ssh", ts, "true"]),
        C("ssh_phases", ["ssh", "-v", ts, "true"], phases=True),
        C("ctx_ps", dk(ts, "ps")),
        C("ctx_inspect", dk(ts, "inspect", probe)),
    ]


def e4(t, cfg):
    """Transport paradigms: stock SSH (overlay / LAN), ControlMaster, mutual TLS."""
    probe = probe_of(cfg, t)
    variants = ["ts", "cm"] + [v for v in ("lan", "tls", "tuned") if context_ok(name(t, v))]
    conds = [C("local_cli_version", dk("default", "version", "--format", FMT), local=True),
             C("local_cli_ps", dk("default", "ps"), local=True)]
    for v in variants:
        c = name(t, v)
        conds += [C(f"{v}_version", dk(c, "version", "--format", FMT)),
                  C(f"{v}_ps", dk(c, "ps")),
                  C(f"{v}_inspect", dk(c, "inspect", probe))]
    return conds


def e5(t, cfg):
    """Payload sweep over a deterministic 100 B/line log."""
    logs = cfg["containers"]["logs"]
    conds = []
    for n_lines in LOG_SIZES:
        n = logs_n(n_lines)
        conds.append(C(f"local_logs_{n_lines}", dk("default", "logs", "--tail", str(n_lines), logs),
                       local=True, needs_container=True, n=n, size=n_lines))
        for v in ("ts", "cm"):
            conds.append(C(f"{v}_logs_{n_lines}", dk(name(t, v), "logs", "--tail", str(n_lines), logs),
                           n=n, size=n_lines))
    return conds


def e7(t, cfg):
    """Network sensitivity; run once per netem level with --tag rtt<ms>. lan_ps (if reachable) is an undelayed control."""
    ts, logs = name(t, "ts"), cfg["containers"]["logs"]
    conds = [
        C("ssh_true", ["ssh", ts, "true"]),
        C("ssh_phases", ["ssh", "-v", ts, "true"], phases=True, n=100),
        C("ts_ps", dk(ts, "ps")),
        C("cm_ps", dk(name(t, "cm"), "ps")),
        C("ts_logs_10000", dk(ts, "logs", "--tail", "10000", logs), n=30, size=10000),
        C("ts_logs_100000", dk(ts, "logs", "--tail", "100000", logs), n=20, size=100000),
    ]
    if context_ok(name(t, "lan")):
        conds.append(C("lan_ps", dk(name(t, "lan"), "ps")))
    if context_ok(name(t, "tuned")):
        conds += [C("tuned_ps", dk(name(t, "tuned"), "ps")),
                  C("tuned_ssh_phases", ["ssh", "-v", name(t, "tuned"), "true"], phases=True, n=100)]
    return conds


def pam(t, cfg):
    """Clean PAM test: same host, binary, user and host keys; only UsePAM differs.
    Needs the temporary no-PAM sshd on the target (port 2222) and the
    sshmf-<t>-nopam alias written by confirm/pam_test.sh."""
    ts, nopam = name(t, "ts"), name(t, "nopam")
    return [
        C("pam_ssh_true", ["ssh", ts, "true"]),
        C("nopam_ssh_true", ["ssh", nopam, "true"]),
        C("pam_ssh_phases", ["ssh", "-v", ts, "true"], phases=True),
        C("nopam_ssh_phases", ["ssh", "-v", nopam, "true"], phases=True),
        C("pam_ctx_ps", dk(ts, "ps")),
        C("nopam_ctx_ps", ["docker", "-H", f"ssh://{nopam}", "ps"]),
    ]


# exp -> (builder, default n, [(mode, schedule, blocks)])
EXPS = {
    "e1": (e1, 500, [("closed", "interleaved", 1)]),
    "e2": (e2, 300, [("closed", "blocked", 3), ("random", "interleaved", 1)]),
    "e3": (e3, 1000, [("closed", "interleaved", 1)]),
    "e4": (e4, 300, [("closed", "interleaved", 1)]),
    "e5": (e5, 50, [("closed", "interleaved", 1)]),
    "e7": (e7, 200, [("closed", "interleaved", 1)]),
    "pam": (pam, 300, [("closed", "interleaved", 1)]),
}


# ---------------------------------------------------------------- scheduling
def build_order(conds, n_default, schedule, blocks, warmup, rng):
    """Returns [(cond, block, iter, is_warmup)]. iter < 0 marks warm-up calls."""
    ns = {c.name: c.n or n_default for c in conds}
    order = []
    if schedule == "interleaved":
        # Randomised complete blocks: every round runs each remaining condition once.
        for i in range(-warmup, max(ns.values())):
            rnd = [c for c in conds if i < ns[c.name]]
            rng.shuffle(rnd)
            order += [(c, 0, i, i < 0) for c in rnd]
    elif schedule == "blocked":
        # Consecutive calls of one condition, so closed-loop callers can phase-lock.
        units = []
        for c in conds:
            base, extra = divmod(ns[c.name], blocks)
            start = 0
            for b in range(blocks):
                k = base + (1 if b < extra else 0)
                units.append([(c, b, i, i < 0) for i in range(-warmup, 0)] +
                             [(c, b, start + i, False) for i in range(k)])
                start += k
        rng.shuffle(units)
        order = [x for u in units for x in u]
    else:
        raise ValueError(schedule)
    return order


OBSERVE_OK = {"e1", "e2", "e3", "e4"}   # read-only Docker calls only


def run(args, cfg):
    builder, n_default, modes = EXPS[args.exp]
    if not args.local_only and observe_only(cfg, args.target):
        if args.exp not in OBSERVE_OK:
            sys.exit(f"{args.target} is observe-only; {args.exp} is not allowed there")
        if not probe_of(cfg, args.target):
            sys.exit(f"set hosts.{args.target}.probe in config.toml to an existing container")
    conds = builder(args.target, cfg)
    if args.local_only:
        conds = [c for c in conds if c.local]
        if not shutil.which("curl"):   # raw-API baselines need curl on this host
            print("note: no curl on this host, raw-API conditions are skipped", file=sys.stderr)
            conds = [c for c in conds if c.argv[0] != "curl"]
        target_label = "local"
    else:
        conds = [c for c in conds if not (c.local and c.needs_container)]
        target_label = args.target
    if args.only:
        keep = set(args.only.split(","))
        conds = [c for c in conds if c.name in keep]
    if args.n:
        for c in conds:
            c.n = args.n
    if args.mode:
        modes = [m for m in modes if m[0] == args.mode]
    if not conds or not modes:
        sys.exit("nothing to run (check --only / --mode / --local-only)")

    host = socket.gethostname()
    run_id = time.strftime("%Y%m%d-%H%M%S")
    stem = f"{host}_to_{target_label}" + (f"_{args.tag}" if args.tag else "")
    raw_path = out_path(cfg, args.exp, run_id, stem)
    meta_path = out_path(cfg, args.exp, run_id, stem, "meta.json")
    seed = args.seed if args.seed is not None else cfg["run"]["seed"]
    rng = random.Random(seed)
    lo, hi = cfg["run"]["random_gap_s"]
    warmup = cfg["run"]["warmup"] if args.warmup is None else args.warmup

    plan = []
    for mode, schedule, blocks in modes:
        plan += [(mode, x) for x in build_order(conds, n_default, schedule, blocks, warmup, rng)]

    if args.dry_run:
        for mode, (c, b, i, w) in plan[: len(conds) * 2]:
            print(f"[{mode}] {c.name:24s} b{b} i{i:<4d} {'W' if w else ' '} {' '.join(c.argv)}")
        print(f"... {len(plan)} calls total -> {raw_path}")
        return

    meta = {"run_id": run_id, "exp": args.exp, "argv": sys.argv, "seed": seed,
            "modes": modes, "conditions": {c.name: " ".join(c.argv) for c in conds},
            "start": snapshot(cfg, None if args.local_only else args.target)}
    write_meta(meta_path, meta)
    w = RawWriter(raw_path)
    base = {"run_id": run_id, "exp": args.exp, "controller": host,
            "target": target_label, "tag": args.tag or ""}
    print(f"{args.exp}: {len(plan)} calls -> {raw_path}")
    t_begin = time.time()
    last_start = float("-inf")
    status = "complete"
    try:
        for seq, (mode, (c, b, i, warm)) in enumerate(plan):
            gap = cfg["run"]["closed_gap_s"] if mode == "closed" else rng.uniform(lo, hi)
            if args.pace:   # at least `pace` seconds between consecutive call starts
                gap = max(gap, args.pace - (time.monotonic() - last_start))
            time.sleep(gap)
            last_start = time.monotonic()
            r = run_phases(c.argv) if c.phases else run_timed(c.argv)
            w.write({**base, **r, "mode": mode, "condition": c.name, "size": c.size,
                     "block": b, "iter": i, "seq": seq, "warmup": int(warm),
                     "gap_ms": round(gap * 1000, 1), "load1": load1()})
            if r["rc"] != 0:
                print(f"  ! {c.name} rc={r['rc']} {r['err_head'][:120]}", file=sys.stderr)
            if seq % 100 == 0 or seq == len(plan) - 1:
                el = time.time() - t_begin
                eta = el / (seq + 1) * (len(plan) - seq - 1)
                print(f"  {seq + 1}/{len(plan)}  {c.name} {r['dur_ms']:.1f} ms  "
                      f"elapsed {el / 60:.1f} min  eta {eta / 60:.1f} min", flush=True)
    except KeyboardInterrupt:
        status = "interrupted"
        print("interrupted; rows so far are saved", file=sys.stderr)
    finally:
        w.close()
        meta["status"] = status
        meta["end"] = snapshot(cfg, None if args.local_only else args.target)
        write_meta(meta_path, meta)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exp", choices=sorted(EXPS))
    ap.add_argument("--target", default="ume", help="target host name in config.toml")
    ap.add_argument("--local-only", action="store_true", help="run only local conditions (use on the target)")
    ap.add_argument("--n", type=int, help="override calls per condition (all conditions)")
    ap.add_argument("--mode", choices=["closed", "random"], help="run only one arrival mode")
    ap.add_argument("--only", help="comma-separated condition names")
    ap.add_argument("--tag", help="free label, e.g. rtt20 for E7")
    ap.add_argument("--warmup", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--pace", type=float, default=0.0,
                    help="minimum seconds between call starts (rate-limited paths)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(args, load_config())


if __name__ == "__main__":
    main()
