#!/usr/bin/env bash
# Add delay ONLY to packets from this host to one destination IP (E7).
# Other users' traffic on the interface is not delayed. Run on the controller.
#
#   sudo setup/netem.sh apply <delay_ms> <dst_ip> [iface=tailscale0] [ttl=3h]
#   sudo setup/netem.sh clear [iface=tailscale0]
#   setup/netem.sh show [iface=tailscale0]
#
# `apply` also schedules an automatic clear after <ttl> as a safety net.
set -euo pipefail
cmd=${1:-}

need_root() { [ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }; }

case "$cmd" in
  apply)
    need_root
    delay=${2:?delay_ms}; dst=${3:?dst_ip}; ifc=${4:-tailscale0}; ttl=${5:-3h}
    tc qdisc del dev "$ifc" root 2>/dev/null || true
    tc qdisc add dev "$ifc" root handle 1: prio
    tc qdisc add dev "$ifc" parent 1:3 handle 30: netem delay "${delay}ms"
    tc filter add dev "$ifc" protocol ip parent 1:0 prio 3 u32 match ip dst "${dst}/32" flowid 1:3
    systemctl stop sshmf-netem-clear.timer 2>/dev/null || true
    systemd-run --quiet --unit sshmf-netem-clear --on-active="$ttl" \
      /usr/sbin/tc qdisc del dev "$ifc" root
    echo "netem +${delay}ms to ${dst} on ${ifc}; auto-clear in ${ttl}"
    ping -c 5 -q "$dst" | tail -1
    ;;
  clear)
    need_root
    ifc=${2:-tailscale0}
    tc qdisc del dev "$ifc" root 2>/dev/null || true
    systemctl stop sshmf-netem-clear.timer 2>/dev/null || true
    tc qdisc show dev "$ifc"
    ;;
  show)
    ifc=${2:-tailscale0}
    tc qdisc show dev "$ifc"; tc filter show dev "$ifc" 2>/dev/null || true
    ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
