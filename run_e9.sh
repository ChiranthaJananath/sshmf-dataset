#!/usr/bin/env bash
# E9: SSH-MF v2 controller vs stock agentless control, run on kiri.
# Four variants x two blocks in ABBA order (naive reuse batch v2 | v2 batch reuse naive),
# each block a supervision loop over ume, botan, keyaki and sugi with injected faults.
#
#   ./run_e9.sh --quick    2-min blocks (~20 min), data in data/quick
#   ./run_e9.sh            10-min blocks (~1.5 h), data in data/raw
#   ./run_e9.sh --fast     same-CPU comparison (~45 min): naive @10 s vs v2 @1 s, both reading a
#                          CPU-capped busy container (validates v2's controller-side CPU%)
# Re-running skips completed blocks (state .state/<mode>/e9_*).
set -uo pipefail
cd "$(dirname "$0")"
HOSTS=${HOSTS:-ume,botan,keyaki,sugi}
FAST=0
if [ "${1:-}" = "--quick" ]; then
  MODE=quick; export SSHMF_DATA_DIR=data/quick; MIN=2; PERIOD=5; FAULT=20
elif [ "${1:-}" = "--fast" ]; then
  MODE=full; export SSHMF_DATA_DIR=data/raw; MIN=10; PERIOD=10; FAULT=60; FAST=1
else
  MODE=full;  export SSHMF_DATA_DIR=data/raw;   MIN=10; PERIOD=10; FAULT=60
fi
STATE=.state/$MODE; mkdir -p "$STATE" logs results "$SSHMF_DATA_DIR" || exit 1
LOG=logs/run_e9_${MODE}_$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
[ "$(id -u)" != 0 ] || { echo "!! run as <user>, not root"; exit 1; }

echo "== E9 ($MODE) hosts=$HOSTS blocks=${MIN} min period=${PERIOD}s faults~${FAULT}s; log $LOG"
for h in ${HOSTS//,/ }; do      # the contexts were created by run_all.sh; check they still answer
  docker --context "sshmf-$h-ts" version --format '{{.Server.Version}}' >/dev/null 2>&1 \
    || { echo "!! context sshmf-$h-ts does not answer; run: python3 sshmf_setup.py contexts --target $h"; exit 1; }
  ssh -n -o BatchMode=yes "sshmf-$h-cm" true || { echo "!! alias sshmf-$h-cm does not work"; exit 1; }
done
echo "   all contexts answer"

FAILED=()
b=0
if [ $FAST = 1 ]; then PLAN="naive:10 v2:1 v2:1 naive:10"; EXTRA="--probe sshmf-busy"; TAG=fast
else PLAN="naive reuse batch v2 v2 batch reuse naive"; EXTRA=""; TAG=""; fi
N=$(echo $PLAN | wc -w)
for item in $PLAN; do
  v=${item%%:*}; p=$PERIOD; [ "$item" != "$v" ] && p=${item#*:}
  b=$((b + 1)); blk=$(( b <= N / 2 ? 1 : 2 ))
  id="e9${TAG:+_$TAG}_${v}_b${blk}"
  if [ -f "$STATE/$id" ]; then echo "-- skip $id (already done)"; continue; fi
  echo; echo "== [$(date +%H:%M:%S)] $id (period ${p}s)"
  # shellcheck disable=SC2086
  if python3 e9_controller.py --variant "$v" --block "${TAG:+$TAG}$blk" --hosts "$HOSTS" --minutes "$MIN" \
       --period "$p" --fault-every "$FAULT" $EXTRA; then
    touch "$STATE/$id"
  else
    FAILED+=("$id")
  fi
  sleep 30   # let every target settle between blocks
done

if [ $FAST = 1 ]; then   # stop the CPU-capped busy containers
  for h in ${HOSTS//,/ }; do docker --context "sshmf-$h-ts" rm -f sshmf-busy >/dev/null 2>&1; done
fi
TS=$(date +%Y%m%d-%H%M%S)
BUNDLE=results/sshmf_e9_${MODE}${TAG:+_$TAG}_$TS.tar.gz
tar czf "$BUNDLE" "$SSHMF_DATA_DIR/e9" "$LOG"
echo "bundle: $BUNDLE"
if [ ${#FAILED[@]} -gt 0 ]; then echo "failed blocks: ${FAILED[*]} (rerun to retry)"; exit 1; fi
echo "all blocks completed. Send me: $BUNDLE"
