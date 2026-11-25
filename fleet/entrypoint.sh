#!/bin/sh
set -e
cp /authorized_keys /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
ssh-keygen -A >/dev/null
/usr/sbin/sshd -e
exec dockerd-entrypoint.sh "$@"
