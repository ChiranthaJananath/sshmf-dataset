#!/usr/bin/env bash
# Key-exchange microbenchmark, run on kiri (~15 min). Compares
#   sntrup761x25519 (OpenSSH 9.0-9.9 default; Ubuntu 24.04 LTS)
#   mlkem768x25519  (OpenSSH 10 default, post-quantum)
#   curve25519      (classical)
# Key exchange is CPU work, so every scenario runs client and server on the SAME host
# over loopback (<redacted-ip>:2201, sshd in a container with --network host). No network
# path or firewall is involved, and only the client's software/CPU differ between rows:
#   A: kiri, OpenSSH 10 client (container)    B: ume, OpenSSH 10 client (container)
#   C: kiri, its own OpenSSH 9.6 client       D: ume, its own OpenSSH 9.6 client
# (9.6 has no ML-KEM.) The temporary containers are named sshmf-kexd and removed at the end.
set -euo pipefail
cd "$(dirname "$0")/.."
N=${N:-200}; P=2201; UME=sshmf-ume-ts
KEX_ALL="sntrup761x25519-sha512@openssh.com,mlkem768x25519-sha256,curve25519-sha256"
KEX_OLD="sntrup761x25519-sha512@openssh.com,curve25519-sha256"
OUT=data/raw/kex/$(date +%Y%m%d-%H%M%S); mkdir -p "$OUT"
cleanup() {
  docker --context default rm -f sshmf-kexd >/dev/null 2>&1 || true
  docker --context "$UME" rm -f sshmf-kexd >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== building the benchmark image on kiri and ume"
docker --context default build -q -t sshmf-kex confirm/kex
docker --context "$UME" build -q -t sshmf-kex confirm/kex
KIRI_PUB=$(cat ~/.ssh/id_ed25519.pub)
UME_PUB=$(ssh -n "$UME" 'cat ~/.ssh/id_ed25519.pub')
UME_HOME=$(ssh -n "$UME" 'echo $HOME')

echo "== starting a loopback-only sshd container on each host (<redacted-ip>:$P)"
cleanup
docker --context default run -d --name sshmf-kexd --network host -e PORT=$P -e AUTH_KEY="$KIRI_PUB" sshmf-kex server >/dev/null
docker --context "$UME"  run -d --name sshmf-kexd --network host -e PORT=$P -e AUTH_KEY="$UME_PUB"  sshmf-kex server >/dev/null
sleep 3

client_kiri() { docker --context default run --rm --network host -v "$HOME/.ssh/id_ed25519:/k:ro" sshmf-kex client --host <redacted-ip> --port $P --key /k "$@"; }
client_ume()  { docker --context "$UME" run --rm --network host -v "$UME_HOME/.ssh/id_ed25519:/k:ro" sshmf-kex client --host <redacted-ip> --port $P --key /k "$@"; }
host_ume()    { ssh "$UME" "python3 - --host <redacted-ip> --port $P --key \$HOME/.ssh/id_ed25519 $*" < confirm/kex/kexbench.py; }

echo "== connectivity check (one login per scenario)"
ok() { tail -1 | grep -q ',0,'; }
client_kiri --n 1 --warmup 0 --kex curve25519-sha256 | ok || { echo "!! A: kiri container client cannot log in"; exit 1; }
client_ume  --n 1 --warmup 0 --kex curve25519-sha256 | ok || { echo "!! B: ume container client cannot log in"; exit 1; }
python3 confirm/kex/kexbench.py --host <redacted-ip> --port $P --key ~/.ssh/id_ed25519 --n 1 --warmup 0 --kex curve25519-sha256 | ok \
  || { echo "!! C: kiri OpenSSH 9.6 cannot log in"; exit 1; }
host_ume --n 1 --warmup 0 --kex curve25519-sha256 | ok || { echo "!! D: ume OpenSSH 9.6 cannot log in"; exit 1; }
echo "   all four clients can log in"

echo "== A: kiri, OpenSSH 10 client";  client_kiri --n "$N" --kex "$KEX_ALL" > "$OUT/A_kiri_ossh10.csv"
echo "== B: ume, OpenSSH 10 client";   client_ume  --n "$N" --kex "$KEX_ALL" > "$OUT/B_ume_ossh10.csv"
echo "== C: kiri, OpenSSH 9.6 client"
python3 confirm/kex/kexbench.py --host <redacted-ip> --port $P --key ~/.ssh/id_ed25519 --n "$N" --kex "$KEX_OLD" > "$OUT/C_kiri_ossh96.csv"
echo "== D: ume, OpenSSH 9.6 client";  host_ume --n "$N" --kex "$KEX_OLD" > "$OUT/D_ume_ossh96.csv"

echo "== summary (median ms, warm-up excluded)"
python3 - "$OUT" <<'PY'
import csv, glob, statistics, sys
for f in sorted(glob.glob(sys.argv[1] + "/*.csv")):
    rows = [r for r in csv.DictReader(open(f)) if int(r["round"]) >= 0]
    print(f.split("/")[-1], "|", rows[0]["client_ssh"] if rows else "")
    for k in dict.fromkeys(r["kex"] for r in rows):
        g = [r for r in rows if r["kex"] == k]; ok = [r for r in g if r["rc"] == "0"]
        km = [float(r["kex_ms"]) for r in ok if r["kex_ms"]]
        tot = statistics.median(float(r["dur_ms"]) for r in ok) if ok else float("nan")
        print(f"   {k:36s} ok={len(ok):3d}/{len(g):3d}  total={tot:7.1f}  kex={statistics.median(km) if km else float('nan'):6.1f}")
PY
echo "data: $OUT"
