#!/usr/bin/env bash
# Clean PAM test, run on kiri WHILE the temporary no-PAM sshd runs on ume (port 2222).
# Compares ume's normal sshd (UsePAM yes, port <ssh-port>) with a second instance of the SAME
# binary, config, host keys and user, differing only in UsePAM=no. ~10 min.
set -euo pipefail
cd "$(dirname "$0")/.."
IP=$(python3 -c "import tomllib;print(tomllib.load(open('config.toml','rb'))['hosts']['ume']['ts_ip'])")
CONF=~/.ssh/config.d/sshmf-nopam.conf
cat > "$CONF" <<CFG
Host sshmf-ume-nopam
  HostName $IP
  Port 2222
  User <user>
  BatchMode yes
  ConnectTimeout 30
  StrictHostKeyChecking accept-new
  UserKnownHostsFile ~/.ssh/known_hosts_sshmf_nopam
  ControlMaster no
  ControlPath none
CFG
chmod 600 "$CONF"
trap 'rm -f "$CONF"' EXIT
if ! ssh -n sshmf-ume-nopam true; then
  echo "!! cannot log in to the no-PAM sshd on ume:2222. Is it running? (see the instructions)"; exit 1
fi
echo "no-PAM sshd reachable; starting measurements (6 conditions x 300, interleaved)"
python3 bench.py pam --target ume
f=$(ls -t data/raw/pam/*_to_ume.csv | head -1)
python3 analysis/summarize.py "$f"
echo "data: $f"
