# bootstrap_pdu_passwords.py

Bulk-changes the Raritan PDU `admin` password across a list of IPs, handling first-boot/default-password bootstrap.

## Contents

- [What it does](#what-it-does)
- [What it does not do](#what-it-does-not-do)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Windows setup](#windows-setup)
- [The IP list file](#the-ip-list-file)
- [Passwords](#passwords)
- [Usage](#usage)
- [Command-line options](#command-line-options)
- [Common results](#common-results)
- [Troubleshooting](#troubleshooting)
- [Exit codes](#exit-codes)
- [Safety notes](#safety-notes)
- [Source references](#source-references)

## What it does

This script automates the first password-setup step for Raritan PDUs. It reads a list of PDU management IP addresses, logs in to each PDU as `admin`, and changes the admin password from the current/default password to a new password.

It's intended to replace the manual first-login password-change step when bringing up multiple PDUs at once.

The only account it changes:

| | |
|---|---|
| Username | `admin` |
| Password | whatever new password you enter when the script runs |

## What it does not do

- Does not change outlet power, or turn outlets on or off
- Does not reboot the PDU
- Does not update firmware (see `bootstrap_pdu_firmware.py`)
- Does not change network settings
- Does not configure SNMP, syslog, NTP, DNS, users, roles, or names
- Does not create or delete accounts
- Does not store the new password in a file

## How it works

For each PDU IP address:

1. **Try the new password first.** This makes the script safe to rerun — if the new password already works, it reports `OK - already_changed` and moves on.
2. **If the new password doesn't work, try the old/current/bootstrap password.** For brand-new PX3-style PDUs, this is commonly `raritan`.
3. **Handle the forced-change response.** If the PDU requires a first-login password change, the API may return HTTP 451. The script treats this as expected — it means the PDU is reachable and the admin account needs its password changed before normal login continues.
4. **Change the password** via the API's `setAccountPassword()` method on the `/auth/user/admin` resource.
5. **Verify the change** by logging in again with the new password.
6. **Report the result** and move to the next PDU. A failure on one PDU doesn't stop the rest.

## Requirements

- Python 3.8+
- The `raritan` Python package: `pip install raritan`
- HTTPS access (usually TCP 443) from your machine to each PDU's management interface
- The current/default admin password for the PDUs
- A `pdus.txt` file listing the PDU IPs

## Windows setup

1. **Install Python.** Download from [python.org/downloads](https://www.python.org/downloads/) and run the installer, enabling **Add python.exe to PATH** if offered. (Alternatively, the Python install manager via `winget install 9NQ7512CXL7T` or the Microsoft Store also works.)
2. **Open PowerShell.** A normal user window is fine; you don't usually need Administrator.
3. **Confirm Python works:**
   ```powershell
   py --version
   ```
   If that doesn't work, try `python --version`. At least one should succeed.
4. **Create a working folder** and put the script there:
   ```powershell
   mkdir C:\pdu-bootstrap
   cd C:\pdu-bootstrap
   ```
5. **Create `pdus.txt`** (see [The IP list file](#the-ip-list-file) below).
6. **Create and activate a virtual environment:**
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   If PowerShell blocks activation with an execution-policy error:
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   .\.venv\Scripts\Activate.ps1
   ```
   Your prompt should now start with `(.venv)`.
7. **Install the Raritan package:**
   ```powershell
   python -m pip install --upgrade pip
   python -m pip install raritan
   python -c "import raritan.rpc; print('raritan import OK')"
   ```

## The IP list file

Default filename: `pdus.txt`, one PDU management IP per line, in the same folder you run the script from (unless you pass `--ips`).

```powershell
notepad .\pdus.txt
```

```text
# Example group of PDUs
10.10.5.10
10.10.5.11
10.10.5.12

# Spare PDU
10.10.5.13
```

Blank lines are ignored. Lines starting with `#` are ignored. Duplicate IPs are skipped automatically.

## Passwords

The script needs two passwords: the **old/current/bootstrap** admin password, and the **new** password you want to set.

The old password can be supplied three ways, in order of preference:

1. Typed at an interactive prompt (safest)
2. The `PDU_OLD_PASSWORD` environment variable
3. `--old-password` on the command line (supported, but visible in shell history — avoid unless you have a specific reason)

The new password is always typed interactively, twice, to catch typos. Nothing is echoed to the screen or written to a file.

**Before changing anything, the script checks that:**

- The new password and its confirmation match
- The new password meets `--min-length` (default 8)
- The new password is not the same as the old/current password

The PDU may enforce stronger password rules than this local check. If the PDU itself rejects the password, the script translates the API's return code into one of the readable names listed under [Common results](#common-results).

**Use an environment variable to avoid retyping the old password:**
```powershell
$env:PDU_OLD_PASSWORD = "raritan"
python .\bootstrap_pdu_passwords.py --ips .\pdus.txt --dry-run
```
This only applies to the current PowerShell session — close the window when done, and avoid this while screen-sharing or recording.

## Usage

**Step 1 — dry run** (checks reachability and the old password only; makes no changes):
```powershell
python .\bootstrap_pdu_passwords.py --ips .\pdus.txt --dry-run -v
```
The script prompts only for the old/default password in dry-run mode — no new password is needed since nothing is being changed.

Good output, one PDU:
```text
2026-07-03 21:56:58 INFO Processing 1 PDU(s) [DRY RUN, concurrency=1, verify_cert=False]...
2026-07-03 21:56:58 INFO 10.10.5.137: OK - would_change
2026-07-03 21:56:58 INFO Done. OK=1 Failed=0
```

The line to look for: `<IP>: OK - would_change` — this means the script reached the PDU with the current/default password and would be able to proceed during a live run.

**Step 2 — live run**, once the dry run looks good:
```powershell
python .\bootstrap_pdu_passwords.py --ips .\pdus.txt --log-file .\pdu-bootstrap.log
```
You'll be prompted for the current password, then the new password (twice).

Good output, one PDU:
```text
2026-07-03 22:05:12 INFO Processing 1 PDU(s) [LIVE, concurrency=1, verify_cert=False]...
2026-07-03 22:05:14 INFO 10.10.5.137: OK - changed
2026-07-03 22:05:14 INFO Done. OK=1 Failed=0
```

**Step 3 — rerun safely, any time.** If a PDU already has the new password, it reports `OK - already_changed` — this is expected and not a problem; the script tried the new password first and confirmed it already works.

**Recommended rollout process:**
1. Put one test PDU in `pdus.txt`.
2. Dry-run it.
3. If that works, run it live.
4. Confirm you can log in to that PDU with the new password.
5. Add the remaining PDU IPs to `pdus.txt`.
6. Dry-run again, then run live again.
7. Save the log file for your records.

**Other common commands:**
```powershell
# Different IP list file
python .\bootstrap_pdu_passwords.py --ips .\my-pdus.txt --dry-run

# Longer timeout
python .\bootstrap_pdu_passwords.py --ips .\pdus.txt --timeout 30 --dry-run

# Process four PDUs in parallel (after you trust the settings)
python .\bootstrap_pdu_passwords.py --ips .\pdus.txt --concurrency 4
```
Keep `--concurrency 1` (the default) for first use.

## Command-line options

| Flag | Default | Description |
|---|---|---|
| `--ips PATH` | `pdus.txt` | IP list file |
| `--old-password PASSWORD` | *(env/prompt)* | Old/current/default admin password. Supported but not recommended on the CLI — can appear in shell history |
| `--timeout SECONDS` | `10` | Connection timeout per PDU |
| `--concurrency NUMBER` | `1` | Number of PDUs processed in parallel. Invalid values like `0` or negative numbers are blocked |
| `--dry-run` | off | Check connectivity and the old password only; makes no changes |
| `--min-length NUMBER` | `8` | Local minimum length check for the new password |
| `-v`, `--verbose` | off | Debug logging |
| `--log-file PATH` | none | Also write logs to this file |
| `--no-insecure` | — | Require valid, trusted HTTPS certificates |
| `--insecure` | on by default | Disable HTTPS certificate verification (default, since most PDUs use self-signed certs) |

## Common results

| Result | Meaning |
|---|---|
| `OK - would_change` | Dry-run succeeded; the script would change this PDU during a live run |
| `OK - changed` | Password changed and verified working |
| `OK - already_changed` | PDU already accepted the new password; nothing needed to change (normal on reruns) |
| `OK - password_unchanged` | PDU reported the password as unchanged; treated as OK, but worth reviewing if unexpected |
| `ERROR - HTTP Error 401 / 403` | The password used for that PDU is probably wrong |
| `ERROR` *(connection timeout / refused / DNS failure)* | The script couldn't reach the PDU at that IP |
| `ERROR - password_too_short` | New password shorter than the PDU's policy |
| `ERROR - password_too_long` | New password longer than the PDU's policy |
| `ERROR - password_empty` | New password rejected as empty |
| `ERROR - password_needs_lowercase` | Needs a lowercase letter |
| `ERROR - password_needs_uppercase` | Needs an uppercase letter |
| `ERROR - password_needs_number` | Needs a numeric character |
| `ERROR - password_needs_special` | Needs a special character |
| `ERROR - password_in_history` | Password was used too recently |
| `ERROR - password_too_short_for_snmp` | Too short for the PDU's SNMP requirements |
| `ERROR - password_has_control_chars` | Contains disallowed control characters |

## Troubleshooting

| Problem | Fix |
|---|---|
| `No IPs found in pdus.txt` | Make sure `pdus.txt` exists in the folder you're running from, with one IP per line |
| `python is not recognized` / `py is not recognized` | Python isn't installed correctly or isn't on PATH — reinstall and enable the PATH option |
| `No module named raritan` | Activate your virtual environment and run `python -m pip install raritan` |
| `HTTP Error 401` or `403` | The old/current password is probably wrong for that PDU — confirm it |
| Connection timed out / refused / no route to host | Check the IP, that the PDU is online, that your network can reach it, and firewall rules for TCP 443 |
| `password_needs_uppercase` / `_number` / `_special` / `too_short` | The new password doesn't meet the PDU's policy — choose a stronger one and rerun |
| Some PDUs changed, others failed | Review the failed IPs printed at the end, fix the issue, and rerun — already-changed PDUs will safely report `already_changed` |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All PDUs processed successfully |
| `1` | Local setup/input problem (no IPs, password mismatch, invalid concurrency) before any PDU was contacted |
| `2` | One or more PDUs failed |
| `130` | Interrupted with Ctrl+C |

## Safety notes

- Run `--dry-run` first, every time, especially against an unfamiliar batch of PDUs.
- Keep `--concurrency 1` for first use.
- Keep `pdus.txt` limited to the intended PDUs.
- Never paste real passwords into tickets, chat, email, or screenshots.
- Prefer the interactive password prompt over `--old-password` on the CLI.
- Close PowerShell after using `$env:PDU_OLD_PASSWORD`.
- Review log files before sharing them — they shouldn't contain passwords, but check anyway.

## Source references

- [Python for Windows documentation](https://docs.python.org/3/using/windows.html)
- [Python downloads](https://www.python.org/downloads/)
- [Python install manager release page](https://www.python.org/downloads/release/pymanager-262/)
- [Raritan Python package on PyPI](https://pypi.org/project/raritan/)
