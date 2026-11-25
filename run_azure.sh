#!/usr/bin/env bash
# Paced wide-area run on the Azure target (dci), from kiri.
#
# The campus perimeter firewall resets every new SSH session after the 19th
# within ~20 s to an Internet host (measured 2026-10-01), while one session per
# ~7 s is never blocked. So:
#   * every condition that opens a NEW SSH session is paced to one call per PACE s;
#   * session-reuse (ControlMaster) conditions share one connection and run unpaced;
#   * set-up phases are separated by pauses so they never form a burst.
# E2 (phase absorption) is not run here: pacing would fix every arrival gap at
# >= 7 s, which removes the phase effect being measured.
#
#   ./run_azure.sh --quick   tiny samples (~30 min), data in data/quick
#   ./run_azure.sh           full paced run (~2.5 h incl. ume pacing control)
# Re-running skips completed steps (state in .state/<mode>, prefix a_).
set -uo pipefail
cd "$(dirname "$0")"
H=$PWD
T=dci; A=sshmf-$T-ts; RDIR=sshmf-harness; PACE=7

if [ "${1:-}" = "--quick" ]; then
  MODE=quick; export SSHMF_DATA_DIR=data/quick; NF=5; NC=20; NL=3
else
  MODE=full;  export SSHMF_DATA_DIR=data/raw;   NF=60; NC=200; NL=15
fi
DATA=$SSHMF_DATA_DIR; STATE=.state/$MODE
if ! mkdir -p "$STATE" logs results "$DATA" 2>/dev/null || [ ! -w . ]; then
  echo "!! $H is not writable by $(id -un)"; exit 1
fi
LOG=logs/run_azure_${MODE}_$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
T0=$(date +%s); FAILED=()
say()  { echo; echo "== [$(date +%H:%M:%S)] $*"; }
cool() { echo "   (pause ${1}s so set-up connections never form a burst)"; sleep "$1"; }
onhost() { ssh -n "$A" "cd ~/$RDIR && SSHMF_DATA_DIR=$DATA $*"; }
step() {
  local id=$1 desc=$2; shift 2
  if [ -f "$STATE/$id" ]; then echo "-- skip $id (already done)"; return 0; fi
  say "$id: $desc"
  if "$@"; then touch "$STATE/$id"; return 0; fi
  echo "!! $id failed"; FAILED+=("$id"); return 1
}
[ "$(id -u)" != 0 ] || { echo "!! run as <user>, not root"; exit 1; }

say "Azure paced run ($MODE): pace ${PACE}s per new SSH session; log $LOG"

setup_azure() {
  python3 sshmf_setup.py ssh-config --target "$T" || return 1;  cool 30
  tar czf - --exclude=./data --exclude=./logs --exclude=./results --exclude=./.state --exclude='./round_*' . |
    ssh "$A" "mkdir -p ~/$RDIR && tar xzf - -C ~/$RDIR" || return 1;  cool 15
  python3 sshmf_setup.py contexts --target "$T" || return 1;  cool 30
  python3 sshmf_setup.py targets  --target "$T" || return 1;  cool 60
}
step a_setup "aliases, harness copy, contexts, containers on $T (paced)" setup_azure || {
  echo "!! set-up failed; see above"; exit 1; }

# Fresh-session conditions: each call is a new SSH login -> paced.
FRESH_E1="ssh_true,ssh_phases,tuned_ssh_phases,ctx_ps,tuned_ctx_ps,ctx_inspect"
step a_e1 "E1 fresh sessions over the WAN (paced, n=$NF)" \
  python3 bench.py e1 --target "$T" --only "$FRESH_E1" --n "$NF" --pace "$PACE"
cool 60
# Pacing control on the LAN: the same paced conditions against ume (no firewall in
# the path). Paced-vs-unpaced on ume isolates any bias from the 7 s idle gaps
# (CPU frequency, cold caches), so the Azure numbers can be compared with E1.
step a_ume_paced "pacing control: same paced E1 conditions on ume (n=$NF)" \
  python3 bench.py e1 --target ume --only "$FRESH_E1" --n "$NF" --pace "$PACE" --tag paced
cool 30
# Session reuse: one master connection, then channels only -> unpaced.
step a_e4_cm "E4 session reuse over the WAN (unpaced, n=$NC)" \
  python3 bench.py e4 --target "$T" --only cm_version,cm_ps,cm_inspect --n "$NC"
ssh -O exit sshmf-$T-cm >/dev/null 2>&1
cool 60
step a_e5 "E5 payload over the WAN, fresh vs reused session (paced, n=$NL)" \
  python3 bench.py e5 --target "$T" --only ts_logs_1000,ts_logs_10000,ts_logs_100000,cm_logs_1000,cm_logs_10000,cm_logs_100000 --n "$NL" --pace "$PACE"
ssh -O exit sshmf-$T-cm >/dev/null 2>&1
cool 60
# Local baselines run ON the VM (no network calls): one long-lived SSH session each.
step a_e1_local "E1 local baseline on $T" onhost "python3 bench.py e1 --local-only --n $NC"
cool 30
step a_e5_local "E5 local baseline on $T" \
  onhost "python3 bench.py e5 --local-only --only local_logs_1000,local_logs_10000,local_logs_100000 --n $NL"
cool 30

rtt() {   # TCP-connect RTT to the VM's SSH port (no SSH session is opened)
  python3 - "$DATA" <<'EOF'
import csv, socket, sys, time, pathlib
out = pathlib.Path(sys.argv[1]) / "azure_rtt" / time.strftime("%Y%m%d-%H%M%S_kiri_to_dci_rtt.csv")
out.parent.mkdir(parents=True, exist_ok=True)
rows = []
for i in range(15):   # paced: the campus firewall also counts plain TCP connects to port 22
    t = time.perf_counter(); s = socket.create_connection(("<redacted-ip>", 22), 5)
    rows.append((i, time.time_ns(), round((time.perf_counter() - t) * 1000, 3))); s.close(); time.sleep(7)
with open(out, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["i", "t_ns", "tcp_connect_ms"]); w.writerows(rows)
ms = sorted(r[2] for r in rows)
print(f"TCP-connect RTT: min {ms[0]:.1f} median {ms[len(ms)//2]:.1f} ms -> {out}")
EOF
}
step a_rtt "real WAN RTT to $T (TCP connect, 15 samples, paced)" rtt
cool 150   # let the firewall's connection counter clear before the collection login

say "collect $T data"
ssh "$A" "cd ~/$RDIR && [ -d $DATA ] && tar czf - $DATA" | tar xzf - -C "$H" || echo "!! nothing collected"

say "summaries"
TS=$(date +%Y%m%d-%H%M%S); OUT=results/azure_${MODE}_$TS; mkdir -p "$OUT"
for e in e1 e4 e5; do
  files=$(ls "$DATA/$e"/*_to_dci*.csv "$DATA/$e"/*DCI_to_local*.csv "$DATA/$e"/*_to_ume_paced.csv 2>/dev/null)
  [ -n "$files" ] || continue
  # shellcheck disable=SC2086
  python3 analysis/summarize.py $files --out "$OUT/summary_$e.csv" > "$OUT/summary_$e.txt"
  echo "  $e -> $OUT/summary_$e.txt"
done
BUNDLE=results/sshmf_azure_${MODE}_$TS.tar.gz
tar czf "$BUNDLE" $(ls -d "$DATA"/e1/*dci* "$DATA"/e1/*DCI* "$DATA"/e1/*_to_ume_paced* "$DATA"/e4/*dci* "$DATA"/e5/*dci* "$DATA"/e5/*DCI* "$DATA"/azure_rtt 2>/dev/null) "$OUT" "$LOG"
echo "bundle: $BUNDLE"

say "done in $(( ($(date +%s) - T0) / 60 )) min"
if [ ${#FAILED[@]} -gt 0 ]; then echo "failed steps: ${FAILED[*]} (rerun to retry only these)"; exit 1; fi
echo "all steps completed. Send me: $BUNDLE"
