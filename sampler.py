#!/usr/bin/env python3
"""Host and per-process-group sampler (stdlib only, reads /proc).

Streams CSV to stdout (default) or --out. Also runs remotely without being
installed:  ssh host 'python3 - --hz 4 --duration 600' < sampler.py
The sampler's own SSH session chain is excluded from the process groups.

Row kinds (columns t_ns,host,kind,key,n,rss_kb,cpu_ticks,v1,v2,v3):
  proc  key=<comm>|_other|_all   n=#processes, rss_kb, cpu_ticks=utime+stime
  mem   key=kb                   v1=used (Total-Free-Buffers-Cached-SReclaimable)
                                 v2=Total-Available, v3=MemTotal
  cpu   key=ticks                v1=busy ticks, v2=total ticks
  net   key=<iface>              v1=rx bytes, v2=tx bytes
"""
import argparse
import csv
import os
import socket
import sys
import time

WATCH = {
    "sshd", "sshd-session", "sshd-auth", "docker", "dockerd", "containerd",
    "containerd-shim", "containerd-shim-runc-v2", "runc", "docker-proxy",
    "node_exporter", "agent", "portainer", "tailscaled", "systemd", "(sd-pam)",
    "systemd-logind", "bash", "sh", "curl", "socat",
}
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
FIELDS = ["t_ns", "host", "kind", "key", "n", "rss_kb", "cpu_ticks", "v1", "v2", "v3"]


def ppid_of(pid):
    with open(f"/proc/{pid}/stat") as f:
        s = f.read()
    return int(s[s.rindex(")") + 2:].split()[1])


def own_chain():
    """This process and its ancestors up to (not including) the daemon whose parent is PID 1."""
    excl, pid = set(), os.getpid()
    while pid > 1:
        parent = ppid_of(pid)
        if parent == 1:
            break
        excl.add(pid)
        pid = parent
    return excl


def scan(excl, every=False):
    groups = {}
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) in excl:
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                s = f.read()
        except OSError:
            continue
        comm = s[s.index("(") + 1:s.rindex(")")]
        rest = s[s.rindex(")") + 2:].split()
        ticks = int(rest[11]) + int(rest[12])
        rss = int(rest[21]) * PAGE_KB
        for key in (comm if (every or comm in WATCH) else "_other", "_all"):
            g = groups.setdefault(key, [0, 0, 0])
            g[0] += 1
            g[1] += rss
            g[2] += ticks
    return groups


def meminfo():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0])
    used = m["MemTotal"] - m["MemFree"] - m["Buffers"] - m["Cached"] - m.get("SReclaimable", 0)
    return used, m["MemTotal"] - m["MemAvailable"], m["MemTotal"]


def cpu():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + v[4]
    return sum(v) - idle, sum(v)


def netdev(ifaces):
    out = {}
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            name = name.strip()
            if name in ifaces:
                d = data.split()
                out[name] = (int(d[0]), int(d[8]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hz", type=float, default=4.0)
    ap.add_argument("--duration", type=float, required=True, help="seconds")
    ap.add_argument("--out")
    ap.add_argument("--ifaces", default="tailscale0,eno8303,enp130s0,lo")
    ap.add_argument("--all", action="store_true", help="report every program, not only WATCH")
    args = ap.parse_args()

    out = open(args.out, "w", newline="") if args.out else sys.stdout
    w = csv.writer(out)
    w.writerow(FIELDS)
    host = socket.gethostname()
    ifaces = set(args.ifaces.split(","))
    excl = own_chain()
    period = 1.0 / args.hz
    t_end = time.monotonic() + args.duration
    nxt = time.monotonic()
    try:
        while time.monotonic() < t_end:
            t = time.time_ns()
            for key, (n, rss, ticks) in sorted(scan(excl, args.all).items()):
                w.writerow([t, host, "proc", key, n, rss, ticks, "", "", ""])
            w.writerow([t, host, "mem", "kb", "", "", "", *meminfo()])
            w.writerow([t, host, "cpu", "ticks", "", "", "", *cpu(), ""])
            for iface, (rx, tx) in netdev(ifaces).items():
                w.writerow([t, host, "net", iface, "", "", "", rx, tx, ""])
            out.flush()
            nxt += period
            time.sleep(max(0.0, nxt - time.monotonic()))
    except (BrokenPipeError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
