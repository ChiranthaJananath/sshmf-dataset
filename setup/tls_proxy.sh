#!/usr/bin/env bash
# Mutual-TLS TCP endpoint for the Docker API (E4 comparison), run ON THE TARGET.
# dockerd itself is NOT reconfigured or restarted: a socat container terminates
# TLS and forwards to /var/run/docker.sock. It binds only to the tailnet IP.
#
#   setup/tls_proxy.sh up <tailnet_ip> [port=2376]
#   setup/tls_proxy.sh down
#
# After `up`, copy ca.pem cert.pem key.pem to the controller, e.g. on kiri:
#   mkdir -p ~/.sshmf/tls-ume && scp -P <ssh-port> '<user>@ume:~/.sshmf/tls/{ca,cert,key}.pem' ~/.sshmf/tls-ume/
set -euo pipefail
export DOCKER_CONTEXT=default   # never follow a non-default current context
DIR=~/.sshmf/tls
NAME=sshmf-tls-proxy

case "${1:-}" in
  up)
    ip=${2:?tailnet_ip}; port=${3:-2376}
    mkdir -p "$DIR"; chmod 700 "$DIR"; cd "$DIR"
    if [ ! -f ca.pem ]; then
      # Follows the Docker "protect the daemon socket" recipe (RSA 4096, 60 days).
      openssl genrsa -out ca-key.pem 4096
      openssl req -new -x509 -days 60 -key ca-key.pem -sha256 -subj "/CN=sshmf-ca" -out ca.pem
      openssl genrsa -out server-key.pem 4096
      openssl req -subj "/CN=sshmf-server" -sha256 -new -key server-key.pem -out server.csr
      printf 'subjectAltName = IP:%s,IP:<redacted-ip>\nextendedKeyUsage = serverAuth\n' "$ip" > extfile.cnf
      openssl x509 -req -days 60 -sha256 -in server.csr -CA ca.pem -CAkey ca-key.pem \
        -CAcreateserial -out server-cert.pem -extfile extfile.cnf
      openssl genrsa -out key.pem 4096
      openssl req -subj "/CN=sshmf-client" -new -key key.pem -out client.csr
      echo "extendedKeyUsage = clientAuth" > extfile-client.cnf
      openssl x509 -req -days 60 -sha256 -in client.csr -CA ca.pem -CAkey ca-key.pem \
        -CAcreateserial -out cert.pem -extfile extfile-client.cnf
      rm -f server.csr client.csr extfile.cnf extfile-client.cnf
      chmod 600 ./*key.pem
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" --label sshmf=1 \
      -p "${ip}:${port}:2376" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$DIR":/tls:ro \
      alpine/socat \
      "OPENSSL-LISTEN:2376,reuseaddr,fork,cert=/tls/server-cert.pem,key=/tls/server-key.pem,cafile=/tls/ca.pem,verify=1" \
      UNIX-CONNECT:/var/run/docker.sock
    echo "mTLS proxy on ${ip}:${port}. Copy ${DIR}/{ca,cert,key}.pem to the controller."
    ;;
  down)
    docker rm -f "$NAME"
    ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac
