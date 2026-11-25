#!/usr/bin/env bash
# Emulated fleet for E8, run ON THE TARGET: N docker-in-docker containers, each
# with its own sshd, published only on the tailnet IP at base_port+i.
#
#   setup/fleet.sh up <N> <tailnet_ip> <controller_pubkey_file> [base_port=22100]
#   setup/fleet.sh down
#
# Then on the controller:  python3 sshmf_setup.py fleet-ssh-config --target <this host> --n <N>
set -euo pipefail
export DOCKER_CONTEXT=default   # never follow a non-default current context
HERE=$(cd "$(dirname "$0")" && pwd)
IMAGE=sshmf-fleet:latest

case "${1:-}" in
  up)
    n=${2:?N}; ip=${3:?tailnet_ip}; key=$(readlink -f "${4:?pubkey_file}"); base=${5:-22100}
    docker build -q -t "$IMAGE" "$HERE/../fleet"
    for i in $(seq 1 "$n"); do
      id=$(printf '%02d' "$i")
      docker rm -f "sshmf-fleet-$id" >/dev/null 2>&1 || true
      docker run -d --name "sshmf-fleet-$id" --label sshmf=1 --privileged --memory 1g \
        -p "${ip}:$((base + i)):22" -v "$key":/authorized_keys:ro "$IMAGE" >/dev/null
      echo "sshmf-fleet-$id -> ${ip}:$((base + i))"
    done
    echo "waiting 30 s for inner dockerd to start..."; sleep 30
    ;;
  down)
    docker ps -aq --filter "name=^sshmf-fleet-" | xargs -r docker rm -f
    ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
