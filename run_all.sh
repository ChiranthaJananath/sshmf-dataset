#!/usr/bin/env bash
# SSH-MF: the whole study from kiri with one command. Nothing is run by hand on ume:
# this script copies the harness to ume over SSH and drives it from here.
#
#   ./run_all.sh --quick     smoke test with tiny samples (~30 min), results in data/quick
#   ./run_all.sh             full study (~10 h), results in data/raw   -> run inside tmux
#
# Options: --no-reverse  skip ume->kiri runs      --no-e7  skip netem runs (needs sudo)
#          --no-comnet   skip the observe-only third target
#          --extra-only  run only the extra targets ([run].extra_targets), not ume/kiri
#          --targets=a,b override the extra-target list    --no-extra  skip extra targets
#          --no-cleanup  keep TLS proxy and fleet  --fresh  forget completed steps
# Re-running after an interruption skips every step already completed.
set -uo pipefail
cd "$(dirname "$0")"
H=$PWD

QUICK=0; REVERSE=1; E7=1; CLEANUP=1; FRESH=0; COMNET=1; EXTRA_ONLY=0; EXTRA=1; TARGETS=""
for a in "$@"; do
  case $a in
    --quick) QUICK=1 ;; --no-reverse) REVERSE=0 ;; --no-e7) E7=0 ;;
    --no-cleanup) CLEANUP=0 ;; --fresh) FRESH=1 ;; --no-comnet) COMNET=0 ;;
    --extra-only) EXTRA_ONLY=1; E7=0; COMNET=0 ;; --no-extra) EXTRA=0 ;;
    --targets=*) TARGETS=${a#--targets=}; TARGETS=${TARGETS//,/ } ;;
    *) sed -n '2,13p' "$0"; exit 1 ;;
  esac
done

cfg() { python3 -c "import tomllib;c=tomllib.load(open('config.toml','rb'));print($1)"; }
UME_TS=$(cfg "c['hosts']['ume']['ts_ip']")
COMNET_PROBE=$(cfg "c['hosts'].get('comnet', {}).get('probe', '')")
[ -n "$TARGETS" ] || TARGETS=$(cfg "' '.join(c['run'].get('extra_targets', []))")
[ $EXTRA = 1 ] || TARGETS=""
UME=sshmf-ume-ts                 # SSH alias created in setup
RDIR=sshmf-harness               # harness copy on ume (under ~)

if [ $QUICK = 1 ]; then
  MODE=quick; export SSHMF_DATA_DIR=data/quick; QN="--n 20"; NFLEET=5; CN="--n 10"
  E6_ARGS="--pre 5 --burst 20 --post 10 --duties 0.2,1.0"
  E8F_ARGS="--Ns 5 --cs 1,5 --rounds 2"; E8S_ARGS="--cs 1,5 --rounds 2"; E7_LEVELS="0 20"
else
  MODE=full; export SSHMF_DATA_DIR=data/raw; QN=""; NFLEET=50; CN="--n 200"
  E6_ARGS=""; E8F_ARGS=""; E8S_ARGS=""; E7_LEVELS="0 10 20 50 100"
fi
DATA=$SSHMF_DATA_DIR
STATE=.state/$MODE
[ $FRESH = 1 ] && rm -rf "$STATE"
if ! mkdir -p "$STATE" logs results "$DATA" 2>/dev/null || [ ! -w . ] || [ ! -w "$DATA" ]; then
  echo "!! $H is not writable by $(id -un). Fix with:  sudo chown -R $(id -un): $H"
  exit 1
fi
LOG=logs/run_all_${MODE}_$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
T0=$(date +%s)
FAILED=()

say()  { echo; echo "== [$(date +%H:%M:%S)] $*"; }
die()  { echo "!! $*"; echo "log: $LOG"; exit 1; }
onume() { ssh -n "$UME" "cd ~/$RDIR && SSHMF_DATA_DIR=$DATA $*"; }
onhost() { local a=$1; shift; ssh -n "$a" "cd ~/$RDIR && SSHMF_DATA_DIR=$DATA $*"; }
push_harness() {   # copy this harness (code only) to ~/$RDIR on the host behind alias $1
  tar czf - --exclude=./data --exclude=./logs --exclude=./results --exclude=./.state --exclude='./round_*' . |
    ssh "$1" "mkdir -p ~/$RDIR && tar xzf - -C ~/$RDIR"
}

# step <id> <description> <command...>: runs once per mode; failures are recorded, not fatal.
step() {
  local id=$1 desc=$2; shift 2
  if [ -f "$STATE/$id" ]; then echo "-- skip $id (already done)"; return 0; fi
  say "$id: $desc"
  if "$@"; then touch "$STATE/$id"; return 0; fi
  echo "!! $id failed"; FAILED+=("$id"); return 1
}

# ------------------------------------------------------------------ checks
say "SSH-MF run_all ($MODE) on $(hostname), log $LOG"
[ "$(id -u)" != 0 ] || die "run as your normal user (<user>), not root"
python3 -c 'import sys; assert sys.version_info >= (3, 11)' || die "python3 >= 3.11 required"
PUBKEY=""
for k in ~/.ssh/id_ed25519.pub ~/.ssh/id_ecdsa.pub ~/.ssh/id_rsa.pub; do
  [ -f "$k" ] && { PUBKEY=$k; break; }
done
[ -n "$PUBKEY" ] || die "no SSH public key in ~/.ssh on kiri"

if [ $E7 = 1 ]; then
  if ! sudo -n true 2>/dev/null; then
    echo "E7 applies tc netem and needs sudo; enter your password once (or rerun with --no-e7)."
    sudo -v || { echo "no sudo: E7 disabled"; E7=0; }
  fi
  if [ $E7 = 1 ]; then
    ( while kill -0 $$ 2>/dev/null; do sudo -n true; sleep 50; done ) &
    trap 'sudo -n setup/netem.sh clear >/dev/null 2>&1' EXIT
  fi
fi

if [ $EXTRA_ONLY = 0 ]; then   # ===== ume / kiri part (skipped with --extra-only) =====
# ------------------------------------------------------------------ setup: forward (kiri -> ume)
say "setup: SSH aliases for ume"
python3 sshmf_setup.py ssh-config --target ume
ssh -n "$UME" true || die "cannot reach ume via alias $UME (key auth over tailnet, port <ssh-port>)"

say "setup: copy harness to ume:~/$RDIR"
push_harness "$UME" || die "copy to ume failed"

step setup_contexts "docker contexts for ume" python3 sshmf_setup.py contexts --target ume || die "contexts"
step setup_targets "probe + log containers on ume" python3 sshmf_setup.py targets --target ume || die "targets"

setup_tls() {
  onume "setup/tls_proxy.sh up $UME_TS" || return 1
  mkdir -p ~/.sshmf/tls-ume
  for f in ca.pem cert.pem key.pem; do scp -q "$UME:.sshmf/tls/$f" ~/.sshmf/tls-ume/ || return 1; done
  chmod 600 ~/.sshmf/tls-ume/key.pem
  python3 sshmf_setup.py tls-context --target ume
}
step setup_tls "mutual-TLS endpoint on ume (for E4)" setup_tls

setup_fleet() {
  scp -q "$PUBKEY" "$UME:kiri_id.pub" || return 1
  onume "setup/fleet.sh up $NFLEET $UME_TS ~/kiri_id.pub" || return 1
  python3 sshmf_setup.py fleet-ssh-config --target ume --n $NFLEET
}
FLEET_OK=1
step setup_fleet "emulated fleet of $NFLEET on ume (for E8)" setup_fleet || FLEET_OK=0

# ------------------------------------------------------------------ setup: reverse (ume -> kiri)
setup_reverse() {
  local key
  key=$(onume "[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519; cat ~/.ssh/id_ed25519.pub") || return 1
  touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
  grep -qF "$key" ~/.ssh/authorized_keys || echo "$key" >> ~/.ssh/authorized_keys
  onume "python3 sshmf_setup.py ssh-config --target kiri" || return 1
  onume "ssh -n sshmf-kiri-ts true" || return 1
  onume "python3 sshmf_setup.py contexts --target kiri" || return 1
  onume "python3 sshmf_setup.py targets --target kiri"
}
if [ $REVERSE = 1 ]; then
  step setup_reverse "keys, aliases, contexts and containers for ume -> kiri" setup_reverse || REVERSE=0
fi

say "preflight"
python3 sshmf_setup.py preflight --target ume || die "preflight has FAIL items (see above)"

# ------------------------------------------------------------------ experiments: forward
step e1        "E1 alpha decomposition (kiri -> ume)"    python3 bench.py e1 --target ume $QN
step e1_local  "E1 local baseline on ume"                onume "python3 bench.py e1 --local-only $QN"
step e2        "E2 phase absorption (kiri -> ume)"       python3 bench.py e2 --target ume $QN
step e2_local  "E2 local baseline on ume"                onume "python3 bench.py e2 --local-only $QN"
step e3        "E3 tail (kiri -> ume)"                   python3 bench.py e3 --target ume $QN
step e4        "E4 transport paradigms (kiri -> ume)"    python3 bench.py e4 --target ume $QN
step e5        "E5 payload sweep (kiri -> ume)"          python3 bench.py e5 --target ume $QN
step e5_local  "E5 local baseline on ume"                onume "python3 bench.py e5 --local-only $QN"
step e6        "E6 footprint vs duty cycle"              python3 e6_footprint.py --target ume $E6_ARGS

run_e7() {
  local d=$1 rc
  if [ "$d" -gt 0 ]; then sudo -n setup/netem.sh apply "$d" "$UME_TS" || return 1; fi
  python3 bench.py e7 --target ume --tag "rtt$d" $QN; rc=$?
  sudo -n setup/netem.sh clear >/dev/null 2>&1
  return $rc
}
if [ $E7 = 1 ]; then
  for d in $E7_LEVELS; do step "e7_rtt$d" "E7 network sensitivity, +${d} ms" run_e7 "$d"; done
fi

[ $FLEET_OK = 1 ] && step e8_fleet "E8 fleet fan-out" python3 e8_fanout.py fleet --target ume $E8F_ARGS
step e8_single "E8 single-sshd concurrency" python3 e8_fanout.py single --target ume $E8S_ARGS

# ------------------------------------------------------------------ experiments: reverse
if [ $REVERSE = 1 ]; then
  step r_e1       "E1 reverse (ume -> kiri)"    onume "python3 bench.py e1 --target kiri $QN"
  step r_e1_local "E1 local baseline on kiri"   python3 bench.py e1 --local-only $QN
  step r_e2       "E2 reverse (ume -> kiri)"    onume "python3 bench.py e2 --target kiri $QN"
  step r_e2_local "E2 local baseline on kiri"   python3 bench.py e2 --local-only $QN
fi
fi   # ===== end of ume / kiri part =====

# ------------------------------------------------------------------ extra targets (full role)
# Same protocol as ume for E1-E5, plus local baselines on each host. kiri is the controller.
setup_extra() {
  local t=$1 a=sshmf-$1-ts
  python3 sshmf_setup.py ssh-config --target "$t" || return 1
  ssh -n "$a" true || { echo "!! cannot reach $t via $a"; return 1; }
  push_harness "$a" || return 1
  python3 sshmf_setup.py contexts --target "$t" || return 1
  python3 sshmf_setup.py targets --target "$t" || return 1
  python3 sshmf_setup.py preflight --target "$t"
}
DONE_EXTRA=""
for T in $TARGETS; do
  if [ -z "$(cfg "c['hosts'].get('$T', {}).get('ts_ip', '')")" ]; then
    echo "-- $T skipped: hosts.$T.ts_ip is empty in config.toml"; continue
  fi
  A=sshmf-$T-ts
  # the harness copy is refreshed on every run, so code fixes reach the target
  if [ -f "$STATE/x_${T}_setup" ]; then push_harness "$A" >/dev/null 2>&1; fi
  step "x_${T}_setup" "aliases, harness copy, contexts, containers, preflight for $T" setup_extra "$T" || continue
  step "x_${T}_e1"       "E1 alpha decomposition (kiri -> $T)" python3 bench.py e1 --target "$T" $QN
  step "x_${T}_e1_local" "E1 local baseline on $T"             onhost "$A" "python3 bench.py e1 --local-only $QN"
  step "x_${T}_e2"       "E2 phase absorption (kiri -> $T)"    python3 bench.py e2 --target "$T" $QN
  step "x_${T}_e2_local" "E2 local baseline on $T"             onhost "$A" "python3 bench.py e2 --local-only $QN"
  step "x_${T}_e3"       "E3 tail (kiri -> $T)"                python3 bench.py e3 --target "$T" $QN
  step "x_${T}_e4"       "E4 transport paths (kiri -> $T)"     python3 bench.py e4 --target "$T" $QN
  step "x_${T}_e5"       "E5 payload sweep (kiri -> $T)"       python3 bench.py e5 --target "$T" $QN
  step "x_${T}_e5_local" "E5 local baseline on $T"             onhost "$A" "python3 bench.py e5 --local-only $QN"
  ssh -O exit "sshmf-$T-cm" >/dev/null 2>&1
  DONE_EXTRA="$DONE_EXTRA $T"
done

# ------------------------------------------------------------------ third target: comnet (observe-only)
# Only read-only Docker calls from kiri; nothing is copied, installed or created on comnet.
setup_comnet() {
  python3 sshmf_setup.py ssh-config --target comnet &&
  python3 sshmf_setup.py contexts --target comnet &&
  python3 sshmf_setup.py preflight --target comnet
}
if [ $COMNET = 1 ] && [ -z "$COMNET_PROBE" ]; then
  echo "-- comnet skipped: set hosts.comnet.probe in config.toml to enable it"; COMNET=0
fi
if [ $COMNET = 1 ]; then
  step setup_comnet "aliases, contexts and preflight for comnet (observe-only)" setup_comnet || COMNET=0
fi
if [ $COMNET = 1 ]; then
  step c_e1 "E1 on comnet (read-only)" python3 bench.py e1 --target comnet $CN
  step c_e2 "E2 on comnet (read-only)" python3 bench.py e2 --target comnet $CN
  step c_e3 "E3 on comnet (read-only)" python3 bench.py e3 --target comnet $CN
  step c_e4 "E4 on comnet (read-only)" python3 bench.py e4 --target comnet $CN
  ssh -O exit sshmf-comnet-cm >/dev/null 2>&1
fi

# ------------------------------------------------------------------ collect, summarise, bundle
collect() {   # pull the target-side data ($DATA) from alias $1 into this harness
  ssh "$1" "cd ~/$RDIR && [ -d $DATA ] && tar czf - $DATA" | tar xzf - -C "$H" || echo "!! nothing collected from $1"
}
if [ $EXTRA_ONLY = 0 ]; then say "collect ume data"; collect "$UME"; fi
for T in $DONE_EXTRA; do say "collect $T data"; collect "sshmf-$T-ts"; done

say "summaries (complete runs only)"
TS=$(date +%Y%m%d-%H%M%S)
OUT=results/${MODE}_$TS
mkdir -p "$OUT"
for e in e1 e2 e3 e4 e5 e7; do
  files=$(python3 - "$DATA/$e" <<'EOF'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
for m in sorted(d.glob("*.meta.json")) if d.exists() else []:
    if json.load(open(m)).get("status") == "complete":
        print(str(m).removesuffix(".meta.json") + ".csv")
EOF
)
  [ -n "$files" ] || continue
  # shellcheck disable=SC2086
  python3 analysis/summarize.py $files --out "$OUT/summary_$e.csv" > "$OUT/summary_$e.txt"
  echo "  $e -> $OUT/summary_$e.txt"
done

BUNDLE=results/sshmf_${MODE}_$TS.tar.gz
tar czf "$BUNDLE" "$DATA" "$OUT" "$LOG"
echo "bundle: $BUNDLE"

# ------------------------------------------------------------------ cleanup
if [ $CLEANUP = 1 ] && [ $EXTRA_ONLY = 0 ]; then
  say "cleanup (TLS proxy, fleet, SSH masters, netem)"
  onume "setup/tls_proxy.sh down; setup/fleet.sh down" >/dev/null 2>&1
  ssh -O exit sshmf-ume-cm >/dev/null 2>&1
  [ $E7 = 1 ] && sudo -n setup/netem.sh clear >/dev/null 2>&1
  rm -f "$STATE"/setup_tls "$STATE"/setup_fleet
fi

say "done in $(( ($(date +%s) - T0) / 60 )) min"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "failed steps: ${FAILED[*]}  (rerun ./run_all.sh$([ $QUICK = 1 ] && echo ' --quick') to retry only these)"
  exit 1
fi
echo "all steps completed. Send me: $BUNDLE"
