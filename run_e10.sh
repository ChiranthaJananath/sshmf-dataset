#!/usr/bin/env bash
# E10: controller scaling over the emulated fleet (fleet_ex/up.sh), run on kiri.
# For every fleet size N, the four variants of e9_controller.py in two blocks in ABBA
# order at a 10 s period, then v2 at a 1 s period in two blocks.
#
#   ./run_e10.sh --quick    2-min blocks, N=10 only (~25 min), data in data/e10_quick
#   ./run_e10.sh            10-min blocks, N=10,25,50,100 (~7.5 h), data in data/e10
# Narrow a run with NS="50 100" and/or SLOW=0 (SLOW=0 skips naive and reuse, whose cycle
# over 100 hosts takes minutes). Re-running skips completed blocks (.state/<mode>/e10_*).
# The controller stores its files under <data dir>/e9/; N is in the block label.
set -uo pipefail
cd "$(dirname "$0")"
if [ "${1:-}" = "--quick" ]; then
  MODE=e10_quick; MIN=2; FAULT=20; NS=${NS:-10}
else
  MODE=e10; MIN=10; FAULT=60; NS=${NS:-10 25 50 100}
fi
export SSHMF_DATA_DIR=data/$MODE
SLOW=${SLOW:-1}
STATE=.state/$MODE; mkdir -p "$STATE" logs results "$SSHMF_DATA_DIR" || exit 1
LOG=logs/run_${MODE}_$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
[ "$(id -u)" != 0 ] || { echo "!! run as <user>, not root"; exit 1; }

NMAX=0; for n in $NS; do [ "$n" -gt "$NMAX" ] && NMAX=$n; done
echo "== E10 ($MODE) N={$NS} blocks=${MIN} min faults~${FAULT}s slow=$SLOW; log $LOG"
python3 e10_setup.py check --n "$NMAX" || { echo "!! fleet not ready; run: python3 e10_setup.py config --n $NMAX"; exit 1; }

if [ "$SLOW" = 1 ]; then PLAN="naive:10 reuse:10 batch:10 v2:10 v2:10 batch:10 reuse:10 naive:10 v2:1 v2:1"
else PLAN="batch:10 v2:10 v2:10 batch:10 v2:1 v2:1"; fi

FAILED=()
for n in $NS; do
  HOSTS=$(seq -f 'ex%03g' 1 "$n" | paste -sd, -)
  seen=""
  for item in $PLAN; do
    v=${item%%:*}; p=${item#*:}
    seen="$seen $item"; blk=$(echo $seen | tr ' ' '\n' | grep -cx "$item")   # 1st or 2nd block of this item
    id="e10_n${n}_${v}_p${p}_b${blk}"
    if [ -f "$STATE/$id" ]; then echo "-- skip $id (already done)"; continue; fi
    echo; echo "== [$(date +%H:%M:%S)] $id"
    if python3 e9_controller.py --variant "$v" --block "n${n}p${p}b${blk}" --hosts "$HOSTS" \
         --minutes "$MIN" --period "$p" --fault-every "$FAULT"; then
      touch "$STATE/$id"
    else
      FAILED+=("$id")
    fi
    sleep 30   # let the target settle between blocks
  done
done

TS=$(date +%Y%m%d-%H%M%S)
BUNDLE=results/sshmf_${MODE}_$TS.tar.gz
tar czf "$BUNDLE" "$SSHMF_DATA_DIR" "$LOG"
echo "bundle: $BUNDLE"
if [ ${#FAILED[@]} -gt 0 ]; then echo "failed blocks: ${FAILED[*]} (rerun to retry)"; exit 1; fi
echo "all blocks completed. Send me: $BUNDLE"
