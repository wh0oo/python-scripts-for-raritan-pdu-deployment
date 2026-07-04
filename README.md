# Raritan PDU Automation Scripts (Python)

Python command-line scripts for bulk-managing **Raritan PX3 / PX2 rack PDUs** over their **JSON-RPC API** (`raritan.rpc` / `python3-raritan`). Point a script at a text file of PDU IP addresses and it logs into every device in the list to bootstrap default admin passwords or roll out a firmware image — sequentially or in parallel, with dry-run support and clear per-device pass/fail reporting.

If you're searching for **"Raritan PDU bulk password reset script"**, **"Raritan PDU firmware update automation"**, **"raritan.rpc Python example"**, or **"how to script Raritan JSON-RPC API"**, this repo is built for exactly that use case: onboarding or maintaining a fleet of Raritan PDUs without clicking through the web UI one device at a time.

## Why this repo exists

Raritan PDUs (PX3, PX2, and compatible Legrand/Raritan-branded models) ship a Python JSON-RPC binding (`raritan.rpc`) that's well documented for single-device use, but there isn't much public tooling for **fleet-wide, unattended operations** — e.g., changing the default `admin` password on 200 freshly racked PDUs, or pushing a firmware image to every PDU in a data center row. These scripts fill that gap.

## Scripts in this repo

| Script | Purpose |
|---|---|
| [`bootstrap_pdu_passwords.py`](./bootstrap_pdu_passwords.py) | Bulk-changes the Raritan PDU `admin` password across a list of IPs — handles first-boot/default-password bootstrap (including the HTTP 451 "password change required" flow), skips devices already updated, and reports per-device results. |
| [`bootstrap_pdu_firmware.py`](./bootstrap_pdu_firmware.py) | Checks installed firmware version or uploads and installs a firmware image across a list of Raritan PDUs, with same-version protection, image validity/compatibility checks, and live update-progress polling. |

More scripts will be added to this repo over time as additional bulk PDU management tasks come up (e.g., network config, SNMP settings, outlet naming). Check back or watch this repo for updates.

## Requirements

- Python 3.8+
- The `raritan` Python package (Raritan's official JSON-RPC client library):
```bash
  pip install raritan
```
- Network (HTTPS) access from wherever you run the script to each PDU's management interface
- Admin credentials for the PDUs (default/bootstrap password, or current password)

## Common setup

Both scripts share the same input format: a plain text file, one PDU IP address (or hostname) per line, with `#` for comments and blank lines ignored.

```text
# pdus.txt
10.0.10.11
10.0.10.12
10.0.10.13
```

Pass this file with `--ips pdus.txt` (default filename is `pdus.txt` in the current directory for both scripts).

Both scripts:
- Default to `--concurrency 1` (sequential, one PDU at a time) — raise it for faster runs across large fleets once you trust the settings.
- Default to `--insecure` (skip TLS certificate verification), since most Raritan PDUs use self-signed certificates out of the box. Use `--no-insecure` to require valid certs.
- Support `--dry-run` to preview what would happen without making any changes.
- Support `-v` / `--verbose` for debug-level logging, and `--log-file <path>` to also write logs to a file for audit purposes.
- Read credentials from an environment variable or an interactive `getpass` prompt — passwords are never accepted as plain command-line arguments, to avoid exposure in shell history or process listings.
- Print a per-device `OK` / `ERROR` result, followed by a final summary and a list of any failed IPs, and exit with a non-zero code if any device failed.

## `bootstrap_pdu_passwords.py`

Rotates the `admin` password across a fleet of Raritan PDUs — useful right after unboxing/racking new units that still have the factory default password, or for periodic credential rotation.

```bash
python bootstrap_pdu_passwords.py --ips pdus.txt
```

You'll be prompted for the current/default admin password (or set `PDU_OLD_PASSWORD`) and the new password to set.

Key flags:

| Flag | Default | Description |
|---|---|---|
| `--ips` | `pdus.txt` | File with one PDU IP per line |
| `--old-password` | *(env/prompt)* | Current/default admin password. Prefer the `PDU_OLD_PASSWORD` env var or the interactive prompt over passing this on the CLI |
| `--timeout` | `10` | Connection timeout (seconds) |
| `--concurrency` | `1` | Number of PDUs processed in parallel |
| `--insecure` / `--no-insecure` | insecure on | TLS certificate verification |
| `--dry-run` | off | Check reachability/credentials only; no password is changed |
| `--min-length` | `8` | Minimum accepted length for the new password |
| `-v`, `--verbose` | off | Debug logging |
| `--log-file` | none | Also write logs to this file |

Each PDU is reported as one of: `changed`, `already_changed` (idempotent re-runs), `password_unchanged`, or a specific password-policy rejection reason (e.g. `password_too_short`, `password_needs_special`).

## `bootstrap_pdu_firmware.py`

Checks the currently installed firmware version, or uploads and installs a firmware image, across a fleet of Raritan PDUs. Designed to be conservative by default for unattended firmware rollouts.

Check current firmware across the fleet:
```bash
python bootstrap_pdu_firmware.py --ips pdus.txt --check -v
```

Preview an update without touching any device:
```bash
python bootstrap_pdu_firmware.py --ips pdus.txt --update --image pdu-firmware.bin --dry-run -v
```

Run the update for real:
```bash
python bootstrap_pdu_firmware.py --ips pdus.txt --update --image pdu-firmware.bin -v --log-file pdu-firmware.log
```

Key flags:

| Flag | Default | Description |
|---|---|---|
| `--check` / `--update` | *(required, one of)* | Read-only firmware check, or upload + install |
| `--ips` | `pdus.txt` | File with one PDU IP per line |
| `--image` | none | Firmware image file (required with `--update`) |
| `--password` | *(env/prompt)* | Admin password. Prefer the `PDU_ADMIN_PASSWORD` env var or the interactive prompt over the CLI |
| `--timeout` | `10` | Timeout (seconds) for normal API calls |
| `--update-timeout` | `1800` | Max seconds to wait for the firmware update to report success/failure |
| `--availability-timeout` | `600` | Max seconds to wait for the PDU to respond again after rebooting post-update |
| `--poll-interval` | `10` | Seconds between update-status polls |
| `--concurrency` | `1` | Number of PDUs processed in parallel |
| `--insecure` / `--no-insecure` | insecure on | TLS certificate verification |
| `--dry-run` | off | For `--update`: report what would happen, don't upload or install |
| `--allow-same-version` | off | Proceed even if the image version matches the installed version (otherwise skipped) |
| `-v`, `--verbose` | off | Debug logging |
| `--log-file` | none | Also write logs to this file |

Safety behavior for `--update`:
- An uploaded image that the PDU reports as invalid or incompatible with the device aborts that device's update with a clear error — the update is never started.
- An image that looks like a same-version reinstall is skipped by default (`--allow-same-version` to override).
- An image that looks like a **downgrade** is treated as a failure and the update is not started — downgrades are intentionally not automated by this script; if you need to downgrade a PDU, do it manually per Raritan's guidance.
- Progress is polled live via the PDU's firmware update status endpoint until it reports success, failure, or a timeout.

## Exit codes (both scripts)

| Code | Meaning |
|---|---|
| `0` | All PDUs completed successfully |
| `1` | Bad arguments / setup problem (e.g. missing IP file, password mismatch) before any PDU was contacted |
| `2` | One or more PDUs failed |
| `130` | Interrupted (Ctrl+C) partway through a run |

## Security notes

- Passwords are **never** accepted as bare command-line arguments in either script — they're read from an environment variable or an interactive, hidden `getpass` prompt, to keep them out of shell history and process listings (e.g. `ps aux`).
- TLS certificate verification is off by default (`--insecure`) to match Raritan PDUs' self-signed certificates out of the box. If your fleet uses a trusted internal CA or valid certs, run with `--no-insecure`.
- Always run with `--dry-run` first against a new or unfamiliar batch of PDUs before making live changes.

## Contributing / roadmap

This repo is actively growing. Planned/possible additions include further bulk Raritan PDU management scripts (network settings, SNMP, outlet/user configuration). Issues and pull requests for additional Raritan PDU automation scripts are welcome.
