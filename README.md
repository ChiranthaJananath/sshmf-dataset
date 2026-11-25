# SSH-MF: Dataset and Measurement Harness

This repository contains the dataset and the measurement harness of the paper

> **SSH-MF: From a Login Problem to a Payload Problem in Agentless Monitoring of Docker Fleets over SSH**
> W.M.C.J.T. Kithulwatta and Kohei Watabe
> Graduate School of Science and Engineering, Saitama University, Japan
> Submitted to *Software: Practice and Experience* (Wiley), 2026.

The paper measures, models, and optimises agentless control of Docker hosts over SSH (Docker's SSH
context). It decomposes the cost of one call into its protocol phases, explains the mechanisms behind
it, derives design rules for a monitoring controller (SSH-MF), and evaluates the controller on four
physical hosts and on emulated fleets of up to 100 hosts. The dataset holds the raw measurements of
every experiment in the paper (E1 to E11 and the controlled tests C1 to C3).

## Citation

If you use this dataset or the harness, please cite the paper. The entry will be completed with the
volume, pages, and DOI once the paper is published.

```bibtex
@article{kithulwatta2026sshmf,
  author  = {Kithulwatta, W.M.C.J.T. and Watabe, Kohei},
  title   = {{SSH-MF}: From a Login Problem to a Payload Problem in Agentless Monitoring of Docker Fleets over {SSH}},
  journal = {Software: Practice and Experience},
  year    = {2026},
  note    = {Submitted}
}
```

## Contents

| Path | Contents |
|---|---|
| `data/raw/` | Raw measurements of E1 to E9, E11, and the controlled tests, one folder per experiment |
| `data/e10/e9/` | Raw measurements of E10 (the E10 runs use the controller of E9, hence the folder name) |
| `logs/` | Console logs of the full runs |
| `*.py`, `*.sh`, `sshmf/` | The measurement harness that produced the data (Python 3.11+, standard library only) |
| `setup/`, `fleet/`, `fleet_ex/` | Set-up of the delay emulation, the mutual-TLS endpoint, and the emulated fleets of E8 and E10/E11 |
| `confirm/` | Scripts of the controlled tests C1 to C3 |
| `analysis/` | Summary and analysis scripts |
| `config.example.toml` | Configuration template; copy it to `config.toml` and enter your own hosts |
| `HARNESS.md` | Step-by-step instructions for running the harness |

## Experiments and data folders

| ID | Purpose (as in the paper) | Folder |
|---|---|---|
| E1 | Decomposition of the per-call cost: layered conditions, phase timing, classical key exchange | `data/raw/e1` |
| E2 | Statistics calls in closed and randomised arrival modes, paired local and remote | `data/raw/e2` |
| E3 | Tail behaviour of new sessions | `data/raw/e3` |
| E4 | Transport alternatives: new session, reused session, mutual TLS | `data/raw/e4` |
| E5 | Payload from 5 kB to 50 MB | `data/raw/e5` |
| E6 | Footprint on the target against duty cycle and two resident agents | `data/raw/e6` |
| E7 | Emulated delay of 0, 10, 20, 50, and 100 ms | `data/raw/e7` |
| E8 | Fan-out over 10 to 50 emulated hosts; simultaneous logins to one server | `data/raw/e8` |
| E9 | Controller evaluation with injected faults, four variants, four hosts | `data/raw/e9` |
| E10 | Controller scaling over 10, 25, 50, and 100 emulated hosts, periods of 10 and 1 s | `data/e10/e9` |
| E11 | Footprint rule D6: sessions kept or released while 100 hosts are idle | `data/raw/e11` |
| C1 | Clean PAM test: the same SSH server with and without PAM | `data/raw/pam` |
| C2 | Banner probe: logins every 0.5 s with per-process CPU sampling on the target | `data/raw/tailprobe` |
| C3 | Key-exchange microbenchmark: sntrup761, ML-KEM, X25519 | `data/raw/kex` |
| — | TCP connection time to the cloud target | `data/raw/azure_rtt` |

Host names follow the paper: `kiri` is the controller; `ume`, `botan`, `keyaki`, and `sugi` are the
physical targets; `dci` (reported by Docker as `DCI`) is the cloud target, the Azure virtual machine;
`ex001` to `ex100` are the emulated hosts of E10 and E11. File names have the form
`<run start>_<controller>_to_<target>[_<tag>]`, where `local` denotes a call run on the host itself.

## File formats

* **Call records** (`<run>.csv`, E1 to E5, E7, C1): one row per call with its start and end time
  (ns), duration (ms), exit status, output and error sizes, the start of any error message, the SSH
  phase timestamps (JSON, ms from process start), and the load average of the controller.
  Warm-up calls are marked `warmup = 1`.
* **Run metadata** (`<run>.meta.json`): command line, conditions, software versions, load, CPU
  governor, and clock offset at the start and end of the run.
* **Controller cycles and events** (`*_cycles.csv`, `*_events.csv`, E9 to E11): one row per host and
  cycle with the cycle time, per-host time, success, timeouts, retries, and the observed container
  state; one row per injected fault with its injection, detection, and repair time.
* **Samples** (`*_samples_<host>.csv`, E6, E11): host-wide memory, CPU, and network counters, and
  memory and CPU of the measured processes, sampled on the controller and the target.
* **Fan-out** (`*_calls.csv`, `*_rounds.csv`, E8): one row per call and per round.
* **Banner probe** (C2): `*_probe.csv` holds one row per login; `*_samples.csv` holds the CPU time
  of the login and banner processes on the target.

Memory is in KiB in the raw files; the paper reports it in MiB and GiB. Data volumes and throughput
are in decimal units.

## Redactions

The data were recorded on shared laboratory servers. Before publication, every IP address, user
name, SSH port number, and internal DNS name was replaced by a placeholder (`<redacted-ip>`,
`<user>`, `<ssh-port>`, `<redacted-host>`). The process samples keep only the processes that the
experiments measure, and the private configuration file is replaced by `config.example.toml`.
Trial runs, intermediate bundles, and the data of a host that the paper does not use are left out.
No measured value was changed.

## Running the harness

The harness needs Python 3.11 or later, the OpenSSH client, and the Docker CLI on the controller, and
an SSH server and Docker Engine on each target; nothing is installed on a target. The analysis
scripts in `analysis/analyze_all.py` additionally need `numpy`, `pandas`, and `scipy`.

```bash
cp config.example.toml config.toml        # enter your hosts, user, and SSH port
python3 sshmf_setup.py ssh-config --target <host>
python3 sshmf_setup.py contexts   --target <host>
python3 sshmf_setup.py targets    --target <host>
python3 sshmf_setup.py preflight  --target <host>
python3 bench.py e1 --target <host> --dry-run
```

`HARNESS.md` describes the set-up and experiments E1 to E8, and `run_all.sh`, `run_azure.sh`, `run_e9.sh`, and
`run_e10.sh` run them in the order of the paper. `e11_d6.py` runs E11.

## License

This repository is released under the MIT License; see [LICENSE](LICENSE).

## Contact

W.M.C.J.T. Kithulwatta, Graduate School of Science and Engineering, Saitama University, Japan.
