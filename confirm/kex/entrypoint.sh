#!/bin/sh
# server: sshd (key auth, no PAM) for the client to connect to
# client: run kexbench.py with the remaining arguments
set -e
case "$1" in
  server)
    echo "$AUTH_KEY" > /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys
    echo "sshd: $(ssh -V 2>&1)"
    # loopback only: with --network host this never touches a firewall or another host
    exec /usr/sbin/sshd -D -e -p "${PORT:-2201}" -o ListenAddress=<redacted-ip> \
      -o PermitRootLogin=prohibit-password -o PasswordAuthentication=no -o UseDNS=no ;;
  client)
    shift; exec python3 /kexbench.py "$@" ;;
  *) echo "usage: server | client <args>"; exit 1 ;;
esac
