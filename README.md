# Raritan PDU Automation Scripts

Python command-line scripts for bulk-managing **Raritan PX-series rack PDUs** over the Raritan **JSON-RPC API** using the `raritan` Python package.

These scripts are designed for bootstrapping and maintaining groups of PDUs from a simple text file of management IPs. They support dry-runs, sequential or parallel processing, clear per-device results, logging, and safe failure handling.

## Scripts in this repo

| Script | Purpose | Documentation |
|---|---|---|
| [`bootstrap_pdu_passwords.py`](./bootstrap_pdu_passwords.py) | Bulk-changes the Raritan PDU `admin` password across a list of devices. | [README](./docs/bootstrap_pdu_passwords.md) / [What it does](./docs/pdu_password_script_explanation.txt) / [How to use](./docs/pdu_password_script_how_to_use.txt) |
| [`bootstrap_pdu_names_from_dns.py`](./bootstrap_pdu_names_from_dns.py) | Sets each PDU's user-defined name from reverse DNS. | [README](./docs/bootstrap_pdu_names_from_dns.md) / [What it does](./docs/pdu_names_from_dns_script_explanation.txt) / [How to use](./docs/pdu_names_from_dns_script_how_to_use.txt) |
| [`bootstrap_pdu_firmware.py`](./bootstrap_pdu_firmware.py) | Checks installed firmware versions or uploads and installs a firmware image across a list of devices. | [README](./docs/bootstrap_pdu_firmware.md) / [What it does](./docs/pdu_firmware_script_explanation.txt) / [How to use](./docs/pdu_firmware_script_how_to_use.txt) |
| [`bootstrap_pdu_radius.py`](./bootstrap_pdu_radius.py) | Stages RADIUS server entries and supporting local user accounts across a list of devices. | [README](./docs/bootstrap_pdu_radius.md) / [What it does](./docs/pdu_radius_script_explanation.txt) / [How to use](./docs/pdu_radius_script_how_to_use.txt) |
| [`bootstrap_pdu_timezone_units.py`](./bootstrap_pdu_timezone_units.py) | Sets timezone and display units, including Fahrenheit, feet, and PSI, across a list of devices. | [README](./docs/bootstrap_pdu_timezone_units.md) / [What it does](./docs/pdu_timezone_units_script_explanation.txt) / [How to use](./docs/pdu_timezone_units_script_how_to_use.txt) |

## Requirements

- Python 3.8+
- The `raritan` Python package:

```bash
pip install raritan
```

- HTTPS access from the system running the scripts to each PDU management interface
- Admin credentials for the target PDUs

## Common input file

Scripts use a plain text IP list. The default filename is usually `pdus.txt`.

```text
# pdus.txt
10.10.5.11
10.10.5.12
10.10.5.13
```

Blank lines and comments are ignored. Some scripts also support inline comments:

```text
10.10.5.11  # rack A
10.10.5.12  # rack B
```

## Common behavior

Scripts support:

- `--ips pdus.txt` to choose the target list
- `--dry-run` to preview behavior without making changes, where applicable
- `--concurrency 1` by default for safe sequential processing
- `--insecure` by default because many PDUs use self-signed TLS certificates
- `--no-insecure` to require valid TLS certificates
- `-v` / `--verbose` for debug logging
- `--log-file <path>` to also write logs to a file
- Interactive password prompting or environment-variable based credentials
- Per-device `OK` / `ERROR` output
- A final summary with failed IPs, if any

## Quick examples

Check what password changes would do:

```bash
python bootstrap_pdu_passwords.py --ips pdus.txt --dry-run -v
```

Set PDU names from reverse DNS, dry-run first:

```bash
python bootstrap_pdu_names_from_dns.py --ips pdus.txt --dry-run -v
```

Check installed firmware versions:

```bash
python bootstrap_pdu_firmware.py --ips pdus.txt --check -v
```

Preview a firmware update:

```bash
python bootstrap_pdu_firmware.py --ips pdus.txt --update --image pdu-firmware.bin --dry-run -v
```

Preview RADIUS server and local user staging:

```bash
python bootstrap_pdu_radius.py --ips pdus.txt --config radius_config.json --dry-run -v
```

Preview timezone and display-unit settings:

```bash
python bootstrap_pdu_timezone_units.py --ips pdus.txt --dry-run -v
```

## Script notes

### `bootstrap_pdu_passwords.py`

Bulk-rotates the Raritan PDU `admin` password. It is intended for initial bootstrap work, password rotation, and idempotent re-runs.

The script avoids using full PDU model metadata as a login check because some Raritan Python bindings can fail while decoding model-specific fields. It uses a smaller authenticated user API call instead.

### `bootstrap_pdu_names_from_dns.py`

Sets each PDU's user-defined name from reverse DNS.

For example, a PTR record like:

```text
pdu-row3-a12.example.com
```

would produce the PDU name:

```text
PDU-ROW3-A12
```

The script resolves and validates reverse DNS before contacting each PDU. Devices with missing or unusable reverse DNS are skipped and reported as failed. If multiple devices would derive the same PDU name, all colliding devices are skipped to avoid assigning duplicate names.

This script intentionally uses raw JSON-RPC for the PDU name read/write path instead of the typed `pdumodel.Pdu.getSettings()` wrapper. On tested hardware, the typed wrapper failed while decoding optional settings fields. The script only needs the PDU name, so it reads and writes only that field.

### `bootstrap_pdu_firmware.py`

Checks installed firmware versions or performs firmware updates.

Firmware updates are intentionally conservative:

- Invalid or incompatible images are rejected before the update starts.
- Same-version installs are skipped unless explicitly allowed.
- Downgrades are not automated.
- Update progress is polled and logged until success, failure, or timeout.

### `bootstrap_pdu_radius.py`

Stages RADIUS server entries and supporting local user accounts.

This script is intended as a staging step before a later authentication-policy change. It does not change the active authentication method, does not change authentication order, and does not switch the PDU from local login to RADIUS login.

The script reads RADIUS server and user definitions from a JSON config file. Secrets are not stored in the config file. The PDU admin password, RADIUS shared secret, and new local-user password can be supplied by environment variables or typed interactively.

RADIUS staging is intentionally conservative:

- Dry-run logs in and reads current state, but does not ask for the RADIUS shared secret or new local-user password.
- Live mode asks for confirmation when the RADIUS shared secret or new local-user password is typed interactively.
- The RADIUS shared secret cannot be verified by reading it back from the API, so live mode reapplies RADIUS server settings whenever RADIUS servers are enabled.
- Existing local users are reported as `already_exists` and are not modified.
- New local users are verified after creation by reading the account list again.

### `bootstrap_pdu_timezone_units.py`

Sets the PDU timezone and display-unit preferences.

By default, it sets:

- Timezone: `America/Chicago`
- Temperature: Fahrenheit
- Length/distance: feet
- Pressure: PSI

The script updates both the default display preferences and every existing local user's display preferences unless `--no-apply-users` is used.

This script intentionally uses raw JSON-RPC for the `/datetime` get/set path instead of the typed `datetime.DateTime.getCfg()` / `setCfg()` wrapper. On tested hardware, the typed wrapper could fail while decoding optional NTP-related fields even though the script only needed timezone settings. The script changes only the timezone portion of the raw DateTime config and preserves the rest.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All requested devices completed successfully |
| `1` | Bad arguments or setup problem before device processing |
| `2` | One or more devices failed |
| `130` | Interrupted with Ctrl+C |

## Security notes

- Prefer environment variables or interactive password prompts over command-line password arguments.
- Do not put passwords or shared secrets in JSON config files.
- Be careful with logs. Device names, IPs, DNS names, usernames, and RADIUS server addresses may identify internal infrastructure.
- TLS verification is disabled by default for compatibility with self-signed PDU certificates. Use `--no-insecure` when your PDUs have trusted certificates.
- Always run with `--dry-run` first against a new or unfamiliar group of PDUs.

## Contributing

Issues and pull requests for additional Raritan PDU automation scripts are welcome.
