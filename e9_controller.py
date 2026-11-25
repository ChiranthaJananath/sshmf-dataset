#!/usr/bin/env python3
"""E9: SSH-MF v2 controller vs stock agentless control, in a fleet-supervision loop.

Every PERIOD seconds the controller collects, from every target host:
  containers list, the state of `sshmf-victim`, resource stats of `sshmf-probe`,
  and every LOGS_EVERY-th cycle the last 1,000 lines of `sshmf-logs`.
A fault injector stops `sshmf-victim` on a random host about once per FAULT_EVERY s;
the controller must detect it (victim not running) and repair it (start it again).

Variants (each adds design mechanisms to the previous one):
  naive  : one `docker --context <h>-ts` call per operation (new SSH login each),
           `stats --no-stream`, hosts sequentially, fixed 5 s timeout, no retry
  reuse  : the same CLI calls over a persistent SSH master (ControlMaster)        [D1]
  batch  : reuse + all queries of a host in ONE channel via the Engine API,
           one-shot stats, CPU% computed by the controller across cycles         [D1 D2 D4]
  v2     : batch + all hosts in parallel + latency-adaptive timeout and one retry [D1 D2 D4 D5]

    python3 e9_controller.py --variant v2 --hosts ume,botan,keyaki,sugi --minutes 10
    SSHMF_FAKE=1 python3 e9_controller.py ...   # simulated hosts, for testing the logic
"""
import argparse
import json
import os
import random
import socket
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sshmf.common import RawWriter, load_config, name, out_path, snapshot, write_meta

FAKE = os.environ.get("SSHMF_FAKE") == "1"
VICTIM, PROBE, LOGS = "sshmf-victim", "sshmf-probe", "sshmf-logs"
CYCLE_FIELDS = ["run_id", "variant", "block", "cycle", "t_start_ns", "period_s", "makespan_ms", "late_ms",
                "host", "host_ms", "ok", "timeouts", "retries", "victim_running", "cpu_pct",
                "mem_mb", "log_bytes", "new_ssh_sessions"]
EVENT_FIELDS = ["run_id", "variant", "block", "host", "t_inject_ns", "t_detect_ns", "t_repair_ns",
                "repair_ok"]


# ----------------------------------------------------------------------------- execution
def sh(argv, timeout):
    """Run a command; returns (rc, stdout_bytes, elapsed_ms). rc=-9 on timeout."""
    t0 = time.perf_counter()
    if FAKE:
        return fake(argv, timeout, t0)
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        return p.returncode, p.stdout, (time.perf_counter() - t0) * 1000
    except subprocess.TimeoutExpired:
        return -9, b"", (time.perf_counter() - t0) * 1000


FAKE_STATE = {}
FAKE_LOCK = threading.Lock()


def fake(argv, timeout, t0):
    """Simulated host: latencies like the measured ones, victim state kept in memory."""
    cmd = " ".join(argv)
    host = next((h for h in FAKE_STATE if f"sshmf-{h}-" in cmd), "x")
    reuse = "-cm" in cmd
    base = 0.04 if reuse else 0.29
    if "stats --no-stream" in cmd:
        base += 1.0 + random.random()
    if "@@SSHMF" in cmd or "/logs" in cmd:
        base += 0.01
    time.sleep(min(base * random.uniform(0.9, 1.15), timeout))
    with FAKE_LOCK:
        if " stop " in cmd:
            FAKE_STATE[host] = False
        if " start " in cmd or "/start" in cmd:
            FAKE_STATE[host] = True
        running = FAKE_STATE.get(host, True)
    if "@@SSHMF" in cmd:
        out = (f'\n@@SSHMF:ps\n[{{"Names":["/{VICTIM}"]}}]\n@@SSHMF:victim\n{{"State":{{"Running":{str(running).lower()}}}}}'
               f'\n@@SSHMF:stats\n{{"cpu_stats":{{"cpu_usage":{{"total_usage":{int(time.time()*1e6)}}},'
               f'"system_cpu_usage":{int(time.time()*1e8)},"online_cpus":4}},"memory_stats":{{"usage":5000000}}}}\n')
        return 0, out.encode(), (time.perf_counter() - t0) * 1000
    if "inspect" in cmd:
        return 0, (b"true" if running else b"false"), (time.perf_counter() - t0) * 1000
    if "logs" in cmd:
        return 0, b"x" * 100_000, (time.perf_counter() - t0) * 1000
    if "stats" in cmd:
        return 0, b'{"CPUPerc":"0.01%","MemUsage":"5MiB / 1GiB"}', (time.perf_counter() - t0) * 1000
    return 0, b"[]", (time.perf_counter() - t0) * 1000


# ----------------------------------------------------------------------------- transports
class Naive:
    """Stock agentless control: one docker CLI call (one SSH login) per operation."""
    ctx_variant, timeout, retries = "ts", 5.0, 0

    def __init__(self, hosts):
        self.hosts = hosts

    def ctx(self, h):
        return name(h, self.ctx_variant)

    def call(self, h, args, timeout):
        return sh(["docker", "--context", self.ctx(h), *args], timeout)

    def collect(self, h, want_logs):
        r = {"timeouts": 0, "retries": 0, "log_bytes": 0, "cpu_pct": "", "mem_mb": "", "victim_running": "",
             "new_ssh_sessions": 0, "ok": 1}
        steps = [("ps", ["ps", "--format", "{{json .}}"]),
                 ("victim", ["inspect", "-f", "{{.State.Running}}", VICTIM]),
                 ("stats", ["stats", "--no-stream", "--format", "{{json .}}", PROBE])]
        if want_logs:
            steps.append(("logs", ["logs", "--tail", "1000", LOGS]))
        for key, args in steps:
            rc, out, _ = self.call(h, args, self.timeout)
            r["new_ssh_sessions"] += 1 if self.ctx_variant == "ts" else 0
            if rc == -9:
                r["timeouts"] += 1
            if rc != 0:
                r["ok"] = 0
                continue
            if key == "victim":
                r["victim_running"] = int(out.strip() == b"true")
            elif key == "stats":
                try:
                    j = json.loads(out.decode().strip().splitlines()[-1])
                    r["cpu_pct"] = float(j["CPUPerc"].rstrip("%"))
                    r["mem_mb"] = j["MemUsage"].split("/")[0].strip()
                except (ValueError, KeyError, IndexError):
                    pass
            elif key == "logs":
                r["log_bytes"] = len(out)
        return r

    def repair(self, h):
        rc, _, _ = self.call(h, ["start", VICTIM], 10)
        return rc == 0

    def open(self):
        pass

    def close(self):
        pass


class Reuse(Naive):
    """D1: same CLI calls, carried by one persistent SSH master per host."""
    ctx_variant = "cm"

    def open(self):
        for h in self.hosts:   # start from a clean master so its set-up cost is counted
            sh(["ssh", "-O", "exit", name(h, "cm")], 5)

    def close(self):
        self.open()


class Batch(Reuse):
    """D1+D2+D4: one channel per host per cycle; Engine API; one-shot stats; CPU% from
    consecutive cycles on the controller (one-shot omits the previous sample)."""

    def __init__(self, hosts):
        super().__init__(hosts)
        self.prev = {}

    def remote(self, h, script, timeout):
        return sh(["ssh", name(h, "cm"), script], timeout)

    def collect(self, h, want_logs):
        r = {"timeouts": 0, "retries": 0, "log_bytes": 0, "cpu_pct": "", "mem_mb": "", "victim_running": "",
             "new_ssh_sessions": 0, "ok": 1}
        c = "curl -sS --max-time 4 --unix-socket /var/run/docker.sock"
        script = (f"echo; echo @@SSHMF:ps; {c} http://localhost/containers/json; "
                  f"echo; echo @@SSHMF:victim; {c} http://localhost/containers/{VICTIM}/json; "
                  f"echo; echo @@SSHMF:stats; {c} 'http://localhost/containers/{PROBE}/stats?stream=false&one-shot=true'")
        rc, out, _ = self.attempt(h, script, r)
        if rc != 0:
            r["ok"] = 0
            return r
        parts = {}
        for block in out.decode(errors="replace").split("\n@@SSHMF:")[1:]:
            k, _, body = block.partition("\n")
            parts[k] = body.strip()
        try:
            r["victim_running"] = int(json.loads(parts["victim"])["State"]["Running"])
            s = json.loads(parts["stats"])
            cpu, sysu = s["cpu_stats"]["cpu_usage"]["total_usage"], s["cpu_stats"].get("system_cpu_usage", 0)
            ncpu = s["cpu_stats"].get("online_cpus", 1)
            if h in self.prev and sysu > self.prev[h][1]:
                r["cpu_pct"] = round((cpu - self.prev[h][0]) / (sysu - self.prev[h][1]) * ncpu * 100, 3)
            self.prev[h] = (cpu, sysu)
            r["mem_mb"] = round(s["memory_stats"].get("usage", 0) / 2**20, 2)
        except (KeyError, ValueError, TypeError):
            r["ok"] = 0
        if want_logs:   # payload still travels to the controller, on a second channel
            rc, out, _ = self.attempt(h, f"{c} 'http://localhost/containers/{LOGS}/logs?stdout=1&tail=1000'", r)
            r["log_bytes"] = len(out) if rc == 0 else 0
        return r

    def attempt(self, h, script, r):
        rc, out, ms = self.remote(h, script, self.timeout)
        if rc == -9:
            r["timeouts"] += 1
        return rc, out, ms

    def repair(self, h):
        rc, _, _ = self.remote(h, f"curl -sS -X POST --unix-socket /var/run/docker.sock "
                                  f"http://localhost/containers/{VICTIM}/start", 10)
        return rc == 0


class V2(Batch):
    """D1+D2+D4+D5: Batch, all hosts in parallel, timeout adapted to each host's latency
    (2 x recent median + 200 ms, at least 0.5 s) and one retry before declaring failure."""
    parallel = True

    def __init__(self, hosts):
        super().__init__(hosts)
        self.lat = {h: [] for h in hosts}

    def attempt(self, h, script, r):
        hist = self.lat[h][-20:]
        tmo = max(0.5, 2 * statistics.median(hist) / 1000 + 0.2) if len(hist) >= 5 else self.timeout
        rc, out, ms = self.remote(h, script, tmo)
        if rc == -9:
            r["timeouts"] += 1
            r["retries"] += 1
            rc, out, ms = self.remote(h, script, self.timeout)
            if rc == -9:
                r["timeouts"] += 1
        if rc == 0:
            self.lat[h].append(ms)
        return rc, out, ms


VARIANTS = {"naive": Naive, "reuse": Reuse, "batch": Batch, "v2": V2}


# ----------------------------------------------------------------------------- fault injection
class Faults:
    def __init__(self, hosts, every, rng, events, base):
        self.hosts, self.every, self.rng, self.events, self.base = hosts, every, rng, events, base
        self.open = {}          # host -> dict(t_inject_ns, t_detect_ns)
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def run(self):
        while not self.stop.wait(self.every * self.rng.uniform(0.5, 1.5)):
            with self.lock:
                free = [h for h in self.hosts if h not in self.open]
            if not free:
                continue
            h = self.rng.choice(free)
            rc, _, _ = sh(["docker", "--context", name(h, "ts"), "stop", "-t", "0", VICTIM], 20)
            if rc == 0:
                with self.lock:
                    self.open[h] = {"t_inject_ns": time.time_ns(), "t_detect_ns": None}

    def observe(self, h, running, repair_fn):
        """Called by the controller with the observed victim state of host h."""
        with self.lock:
            f = self.open.get(h)
        if running == 0:
            if f is not None and f["t_detect_ns"] is None:
                f["t_detect_ns"] = time.time_ns()
            ok = repair_fn(h)
            if f is not None:
                f["repair_ok"] = int(ok)
        elif running == 1 and f is not None and f["t_detect_ns"] is not None:
            self.events.write({**self.base, "host": h, "t_inject_ns": f["t_inject_ns"],
                               "t_detect_ns": f["t_detect_ns"], "t_repair_ns": time.time_ns(),
                               "repair_ok": f.get("repair_ok", "")})
            with self.lock:
                self.open.pop(h, None)


# ----------------------------------------------------------------------------- main loop
def ensure_victims(hosts):
    for h in hosts:
        if PROBE == "sshmf-busy":   # 25% of one core, so CPU% readings can be validated
            rc, out, _ = sh(["docker", "--context", name(h, "ts"), "inspect", "-f", "{{.State.Running}}", PROBE], 20)
            if rc != 0 or out.strip() != b"true":
                sh(["docker", "--context", name(h, "ts"), "rm", "-f", PROBE], 20)
                sh(["docker", "--context", name(h, "ts"), "run", "-d", "--name", PROBE, "--label", "sshmf=1",
                    "--cpus", "0.25", "alpine:3.20", "sh", "-c", "while :; do :; done"], 60)
        rc, out, _ = sh(["docker", "--context", name(h, "ts"), "inspect", "-f", "{{.State.Running}}", VICTIM], 20)
        if rc != 0:
            sh(["docker", "--context", name(h, "ts"), "run", "-d", "--name", VICTIM, "--label", "sshmf=1",
                "alpine:3.20", "sleep", "infinity"], 60)
        elif out.strip() != b"true":
            sh(["docker", "--context", name(h, "ts"), "start", VICTIM], 20)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", choices=VARIANTS, required=True)
    ap.add_argument("--hosts", default="ume,botan,keyaki,sugi")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--period", type=float, default=10.0)
    ap.add_argument("--fault-every", type=float, default=60.0)
    ap.add_argument("--logs-every", type=int, default=6)
    ap.add_argument("--block", default="1")
    ap.add_argument("--seed", type=int, default=20261003)
    ap.add_argument("--probe", default="sshmf-probe",
                    help="container whose stats are read; sshmf-busy = CPU-capped busy loop (validates CPU%%)")
    a = ap.parse_args()
    global PROBE
    PROBE = a.probe
    cfg = load_config()
    hosts = a.hosts.split(",")
    if FAKE:
        FAKE_STATE.update({h: True for h in hosts})
    rng = random.Random(f"{a.seed}-{a.variant}-{a.block}")
    run_id = time.strftime("%Y%m%d-%H%M%S")
    stem = f"{socket.gethostname()}_{a.variant}_b{a.block}"
    meta_path = out_path(cfg, "e9", run_id, stem, "meta.json")
    meta = {"run_id": run_id, "argv": sys.argv, "hosts": hosts, "fake": FAKE}
    if not FAKE:
        ensure_victims(hosts)
        meta["start"] = snapshot(cfg, None)
    write_meta(meta_path, meta)
    cyc = RawWriter(out_path(cfg, "e9", run_id, stem + "_cycles"), CYCLE_FIELDS)
    ev = RawWriter(out_path(cfg, "e9", run_id, stem + "_events"), EVENT_FIELDS)
    base = {"run_id": run_id, "variant": a.variant, "block": a.block}

    tr = VARIANTS[a.variant](hosts)
    tr.open()
    faults = Faults(hosts, a.fault_every, rng, ev, base)
    fthread = threading.Thread(target=faults.run, daemon=True)
    fthread.start()
    pool = ThreadPoolExecutor(max_workers=len(hosts))
    cpu0 = os.times()
    t_end = time.monotonic() + a.minutes * 60
    next_start = time.monotonic()
    cycle = 0
    print(f"E9 {a.variant} block {a.block}: {len(hosts)} hosts, period {a.period}s, {a.minutes} min")
    try:
        while time.monotonic() < t_end:
            late = max(0.0, time.monotonic() - next_start) * 1000
            t_ns, t0 = time.time_ns(), time.perf_counter()
            want_logs = cycle % a.logs_every == 0

            def one(h):
                s = time.perf_counter()
                r = tr.collect(h, want_logs)
                r["host_ms"] = round((time.perf_counter() - s) * 1000, 3)
                faults.observe(h, r["victim_running"] if r["victim_running"] != "" else None, tr.repair)
                return h, r

            if getattr(tr, "parallel", False):
                results = list(pool.map(one, hosts))
            else:
                results = [one(h) for h in hosts]
            span = round((time.perf_counter() - t0) * 1000, 3)
            for h, r in results:
                cyc.write({**base, **r, "cycle": cycle, "t_start_ns": t_ns, "period_s": a.period, "makespan_ms": span,
                           "late_ms": round(late, 1), "host": h})
            if cycle % 10 == 0:
                print(f"  cycle {cycle:4d} makespan {span:8.1f} ms  late {late:7.1f} ms  open faults {len(faults.open)}",
                      flush=True)
            cycle += 1
            next_start += a.period
            if next_start < time.monotonic():      # overran: start the next cycle immediately
                next_start = time.monotonic()
            time.sleep(max(0.0, next_start - time.monotonic()))
    except KeyboardInterrupt:
        meta["status"] = "interrupted"
    finally:
        faults.stop.set()
        cpu1 = os.times()
        tr.close()
        pool.shutdown(wait=False)
        cyc.close()
        ev.close()
        meta.setdefault("status", "complete")
        meta["cycles"] = cycle
        meta["controller_cpu_s"] = round((cpu1.user + cpu1.system + cpu1.children_user + cpu1.children_system)
                                         - (cpu0.user + cpu0.system + cpu0.children_user + cpu0.children_system), 3)
        if not FAKE:
            meta["end"] = snapshot(cfg, None)
        write_meta(meta_path, meta)
        print(f"done: {cycle} cycles, controller CPU {meta['controller_cpu_s']} s")


if __name__ == "__main__":
    main()
