"""Shared helpers: config, timed execution, raw CSV output, run metadata."""
import csv
import json
import os
import socket
import subprocess
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FMT = "{{.Server.Version}}"

RAW_FIELDS = [
    "run_id", "exp", "controller", "target", "tag", "mode", "condition", "size",
    "block", "iter", "seq", "warmup", "gap_ms",
    "t_start_ns", "t_end_ns", "dur_ms", "rc", "out_bytes", "err_bytes",
    "err_head", "phases", "load1",
]

# ssh -v markers used to split one SSH invocation into phases (ms from spawn).
SSH_MARKS = [
    ("connected", "Connection established"),
    ("banner", "Remote protocol version"),
    ("kex_done", "SSH2_MSG_NEWKEYS received"),
    ("auth_done", "Authenticated to"),
    ("session", "Entering interactive session"),
    ("cmd_sent", "Sending command"),
    ("exit_status", "exit-status"),
]


def load_config(path=None):
    with open(path or ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def name(target, variant):
    """Docker context name and SSH alias share one name, e.g. sshmf-ume-ts."""
    return f"sshmf-{target}-{variant}"


def probe_of(cfg, target):
    """Container used by inspect/stats: the host's own `probe` (observe-only hosts use an
    existing container) or the harness-created one."""
    h = cfg["hosts"].get(target, {})
    return h.get("probe", "") if h.get("observe_only") else cfg["containers"]["probe"]


def observe_only(cfg, target):
    return cfg["hosts"].get(target, {}).get("observe_only", False)


def dk(context, *args):
    return ["docker", "--context", context, *args]


def load1():
    try:
        with open("/proc/loadavg") as f:
            return f.read().split()[0]
    except OSError:
        return ""


def _err_head(err):
    return err.decode(errors="replace").strip().replace("\n", " | ")[:200]


def run_timed(argv, timeout=180):
    t_wall = time.time_ns()
    t0 = time.perf_counter()
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err = -9, e.stdout or b"", (e.stderr or b"") + b" TIMEOUT"
    dur = (time.perf_counter() - t0) * 1000
    return {
        "t_start_ns": t_wall, "t_end_ns": time.time_ns(), "dur_ms": round(dur, 3),
        "rc": rc, "out_bytes": len(out), "err_bytes": len(err),
        "err_head": _err_head(err) if rc else "", "phases": "",
    }


def run_phases(argv):
    """Run an `ssh -v` command and timestamp the debug markers as they arrive."""
    t_wall = time.time_ns()
    t0 = time.perf_counter()
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    phases, err = {}, []
    for raw in p.stderr:
        now = round((time.perf_counter() - t0) * 1000, 3)
        line = raw.decode(errors="replace")
        err.append(line)
        for key, pat in SSH_MARKS:
            if key not in phases and pat in line:
                phases[key] = now
    rc = p.wait()
    dur = (time.perf_counter() - t0) * 1000
    errb = "".join(err).encode()
    return {
        "t_start_ns": t_wall, "t_end_ns": time.time_ns(), "dur_ms": round(dur, 3),
        "rc": rc, "out_bytes": 0, "err_bytes": len(errb),
        "err_head": _err_head(errb[-400:]) if rc else "", "phases": json.dumps(phases),
    }


class RawWriter:
    def __init__(self, path, fields=RAW_FIELDS):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "a", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=fields, extrasaction="ignore")
        if self.f.tell() == 0:
            self.w.writeheader()

    def write(self, row):
        self.w.writerow(row)
        self.f.flush()

    def close(self):
        self.f.close()


def out_path(cfg, exp, run_id, stem, ext="csv"):
    data_dir = os.environ.get("SSHMF_DATA_DIR") or cfg["run"]["data_dir"]
    return ROOT / data_dir / exp / f"{run_id}_{stem}.{ext}"


def sh(cmd, timeout=40):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"


def clock_offset_ms(alias, samples=5):
    """Lower bound on (remote clock - local clock); error is below one one-way delay."""
    p = subprocess.Popen(
        ["ssh", alias, f"for i in $(seq {samples}); do date +%s.%N; sleep 0.2; done"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    best = None
    for line in p.stdout:
        recv = time.time()
        try:
            off = (float(line) - recv) * 1000
        except ValueError:
            continue
        best = off if best is None else max(best, off)
    p.wait()
    return None if best is None else round(best, 2)


def snapshot(cfg, target=None):
    m = {
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": socket.gethostname(),
        "docker": sh(f"docker --context default version --format '{{{{.Client.Version}}}}/{{{{.Server.Version}}}}'"),
        "docker_current_context": sh("docker context show"),
        "ssh": sh("ssh -V"),
        "kernel": os.uname().release,
        "loadavg": sh("cat /proc/loadavg"),
        "governor": sh("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "git": sh(f"git -C {ROOT} rev-parse --short HEAD") + (
            "-dirty" if sh(f"git -C {ROOT} status --porcelain -- .") else ""),
        "qdisc_tailscale0": sh("tc qdisc show dev tailscale0"),
    }
    if target:
        ts = name(target, "ts")
        m["target"] = target
        m["target_docker"] = sh(f"docker --context {ts} version --format '{FMT}'")
        m["target_loadavg"] = sh(f"ssh {ts} cat /proc/loadavg")
        m["tailscale_ping"] = sh(f"tailscale ping -c 1 {cfg['hosts'][target]['ts_ip']}")
        m["clock_offset_ms_lower_bound"] = clock_offset_ms(ts)
    return m


def write_meta(path, meta):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(meta, f, indent=2)
