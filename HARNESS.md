# SSH-MF measurement harness

## Quick path: one script on kiri

You only run commands on **kiri**, as <user> (not root), inside `tmux`.
The script copies the harness to ume over SSH, runs ume's part remotely,
collects ume's data back, summarises everything and makes one results bundle.

```bash
git clone <this repo> ~/methodology-iv && cd ~/methodology-iv/harness
tmux new -s sshmf
./run_all.sh --quick      # ~30 min smoke test, tiny samples -> check it ends "all steps completed"
./run_all.sh              # full study, ~10 h (forward + reverse)
```
Result: `results/sshmf_<mode>_<time>.tar.gz` (raw data, summaries, log).
If anything stops, run the same command again: completed steps are skipped.
Options: `--no-reverse`, `--no-e7` (no sudo), `--no-cleanup`, `--fresh`.

Optional, once per host: `sudo timedatectl set-ntp true` (ume showed no NTP sync).


### Azure target (paced)
The campus firewall resets the 20th new SSH session within ~20 s to an Internet
host, so the Azure VM (`dci`) runs separately with every new session paced to one
per 7 s; session-reuse calls run unpaced. `./run_azure.sh --quick`, then
`./run_azure.sh` (~1.5 h). Results: `results/sshmf_azure_<mode>_<time>.tar.gz`.

The sections below describe the individual steps that `run_all.sh` automates.

---

Stdlib-only Python 3.11+ (both hosts have 3.12). Run everything as **<user>, not root**.
Forward direction: controller **kiri** → target **ume**. Reverse: controller ume → target kiri.

## 0. One-time setup

**Both hosts**
```bash
git clone <this repo> ~/methodology-iv && cd ~/methodology-iv/harness
sudo timedatectl set-ntp true            # ume showed no NTP sync
timedatectl show -p NTPSynchronized --value   # expect: yes
```

**Reverse-direction keys** (ume → kiri failed under BatchMode):
```bash
# on ume
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519
ssh-copy-id -p <ssh-port> <user>@<redacted-ip>
```

**On the controller (kiri)**
```bash
python3 sshmf_setup.py ssh-config --target ume   # aliases sshmf-ume-{ts,lan,cm}
python3 sshmf_setup.py contexts   --target ume   # docker contexts, same names
python3 sshmf_setup.py targets    --target ume   # sshmf-probe + sshmf-logs (600k x 100 B lines)
python3 sshmf_setup.py preflight  --target ume   # must show no FAIL
```

**mTLS endpoint for E4** (no dockerd restart; socat container bound to the tailnet IP only)
```bash
# on ume
setup/tls_proxy.sh up <redacted-ip>
# on kiri
mkdir -p ~/.sshmf/tls-ume && scp -P <ssh-port> '<user>@<redacted-ip>:~/.sshmf/tls/{ca,cert,key}.pem' ~/.sshmf/tls-ume/
python3 sshmf_setup.py tls-context --target ume
```

**Emulated fleet for E8** (50 dind containers on ume, ~50 GB memory limit total, tailnet-only ports)
```bash
# on kiri
scp -P <ssh-port> ~/.ssh/id_ed25519.pub <user>@<redacted-ip>:~/kiri_id.pub
# on ume
setup/fleet.sh up 50 <redacted-ip> ~/kiri_id.pub
# on kiri
python3 sshmf_setup.py fleet-ssh-config --target ume --n 50
```

## 1. Runs (forward direction, on kiri unless noted)

Run at night, with `tmux`. Always add `--dry-run` first to see the plan.

| Exp | Command | Calls | ≈ time |
|---|---|---|---|
| E1 α decomposition | `python3 bench.py e1 --target ume` | 5,050 | 30 min |
| E1 local on target | *(on ume)* `python3 bench.py e1 --local-only` | 2,525 | 5 min |
| E2 phase absorption | `python3 bench.py e2 --target ume` | 1,860 | 60 min |
| E2 local on target | *(on ume)* `python3 bench.py e2 --local-only` | 1,860 | 60 min |
| E3 tail | `python3 bench.py e3 --target ume` | 4,020 | 25 min |
| E4 paradigms | `python3 bench.py e4 --target ume` | ~3,700 | 25 min |
| E5 payload | `python3 bench.py e5 --target ume` | 740 | 15 min |
| E5 local on target | *(on ume)* `python3 bench.py e5 --local-only` | 370 | 5 min |
| E6 footprint | `python3 e6_footprint.py --target ume` | — | 67 min |
| E7 network sensitivity | see below, 5 levels | 985 / level | 60–90 min |
| E8 fleet fan-out | `python3 e8_fanout.py fleet --target ume` | — | 25 min |
| E8 single-sshd knee | `python3 e8_fanout.py single --target ume` | — | 10 min |

**E7** (delay only on kiri → ume tailnet packets; the LAN path is the untouched control):
```bash
for d in 0 10 20 50 100; do
  [ "$d" -gt 0 ] && sudo setup/netem.sh apply $d <redacted-ip>
  python3 bench.py e7 --target ume --tag rtt$d
  sudo setup/netem.sh clear
done
```

**Reverse direction** (on ume, after `ssh-config`/`contexts`/`targets --target kiri`):
E1, E2, E3, E5 with `--target kiri`, plus the `--local-only` runs on kiri.

## 2. Output

`data/raw/<exp>/<run_id>_<controller>_to_<target>[_tag].csv` — one row per call
(wall-clock start/end ns, `perf_counter` duration, exit code, stdout/stderr bytes,
failure text, ssh phase timestamps, load) plus a `.meta.json` with versions,
load, tailscale path, qdisc, and remote clock offset at start and end.

```bash
python3 analysis/summarize.py data/raw/e1/*.csv --out e1_summary.csv
```

## 3. Cleanup
```bash
# on ume
setup/tls_proxy.sh down; setup/fleet.sh down
docker ps -aq --filter label=sshmf=1 | xargs -r docker rm -f
# on kiri
sudo setup/netem.sh clear
```

## Notes on shared hosts
- kiri and ume host other users' workloads. The harness records load per call
  and interleaves conditions, so slow drift affects all conditions equally.
- The CPU governor is recorded, not changed.
- `netem.sh` delays only packets to one IP and auto-clears after 3 h.
- `e8_fanout.py single` briefly opens up to 50 concurrent SSH connections to ume's
  real sshd; preflight warns if fail2ban is active there.
