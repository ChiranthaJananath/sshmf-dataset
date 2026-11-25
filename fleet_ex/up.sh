#!/usr/bin/env bash
# Emulated fleet for E10, run ON THE TARGET: N containers ssh-ex-<i>, each an Ubuntu host
# with its own sshd (PAM on) and its own dockerd, published only on the tailnet IP at
# base_port+i. Unlike setup/fleet.sh (E8: Alpine, root, no PAM) these log in as the
# non-root user `exp` and carry the containers that e9_controller.py supervises.
#
#   fleet_ex/up.sh up <N> <tailnet_ip> <controller_pubkey_file> [base_port=22000]
#   fleet_ex/up.sh down
#
# Then on the controller:  python3 e10_setup.py config --target <this host> --n <N>
set -euo pipefail
export DOCKER_CONTEXT=default   # never follow a non-default current context
HERE=$(cd "$(dirname "$0")" && pwd)
IMAGE=sshmf-fleet-ex:latest

case "${1:-}" in
  up)
    n=${2:?N}; ip=${3:?tailnet_ip}; key=$(readlink -f "${4:?pubkey_file}"); base=${5:-22000}
    grep -q '^ssh-' "$key" || { echo "!! $key does not contain a public key"; exit 1; }
    if [ ! -f "$HERE/alpine.tar" ]; then   # inner daemons load it; no pull per emulated host
      docker pull -q alpine:3.20 && docker save alpine:3.20 -o "$HERE/alpine.tar"
    fi
    docker build -q -t "$IMAGE" "$HERE"
    for i in $(seq 1 "$n"); do
      docker rm -fv "ssh-ex-$i" >/dev/null 2>&1 || true
      docker run -d --name "ssh-ex-$i" --hostname "ssh-ex-$i" --label ssh-ex=1 --privileged \
        --memory 1g --cpus 1 -p "${ip}:$((base + i)):22" -v "$key":/authorized_keys:ro "$IMAGE" >/dev/null
      echo "ssh-ex-$i -> ${ip}:$((base + i))"
    done
    echo "waiting for the inner daemons and containers..."
    for _ in $(seq 1 60); do
      ready=0
      for i in $(seq 1 "$n"); do
        s=$(docker exec "ssh-ex-$i" docker inspect -f '{{.State.Running}}' sshmf-logs 2>/dev/null || true)
        [ "$s" = true ] && ready=$((ready + 1))
      done
      [ "$ready" = "$n" ] && break
      sleep 5
    done
    echo "$ready/$n emulated hosts ready"
    [ "$ready" = "$n" ]
    ;;
  down)
    docker ps -aq --filter label=ssh-ex=1 | xargs -r docker rm -fv >/dev/null
    echo "fleet removed"
    ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
