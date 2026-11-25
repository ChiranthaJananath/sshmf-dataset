#!/bin/bash
# Entry point of one emulated host: key, host key, inner dockerd, the three containers
# that e9_controller.py expects (sshmf-probe, sshmf-victim, sshmf-logs), then sshd.
LOG_LINES=${LOG_LINES:-2000}          # 100-byte lines; the controller fetches the last 1,000
PAD=$(printf 'x%.0s' $(seq 1 91))     # "%07d " (8 B) + 91 B + "\n" = 100 B per line

mkdir -p /run/sshd
install -d -m 700 -o exp -g exp /home/exp/.ssh
install -m 600 -o exp -g exp /authorized_keys /home/exp/.ssh/authorized_keys
if [ ! -f /etc/ssh/.sshmf-keys ]; then   # one fresh host key per emulated host
  rm -f /etc/ssh/ssh_host_*
  ssh-keygen -A >/dev/null
  touch /etc/ssh/.sshmf-keys
fi

rm -f /var/run/docker.pid
dockerd >/var/log/dockerd.log 2>&1 &
for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker image inspect alpine:3.20 >/dev/null 2>&1 || docker load -q -i /alpine.tar

have() { docker inspect "$1" >/dev/null 2>&1; }
have sshmf-probe  || docker run -d --name sshmf-probe  --label sshmf=1 alpine:3.20 sleep infinity
have sshmf-victim || docker run -d --name sshmf-victim --label sshmf=1 alpine:3.20 sleep infinity
have sshmf-logs   || docker run -d --name sshmf-logs   --label sshmf=1 --log-driver json-file alpine:3.20 \
  sh -c "awk 'BEGIN{for(i=0;i<$LOG_LINES;i++) printf \"%07d $PAD\\n\", i}'; sleep infinity"
docker start sshmf-probe sshmf-victim sshmf-logs >/dev/null 2>&1

exec /usr/sbin/sshd -D -e
