# bootstrap_pdu_timezone_units.py

Configures Raritan PDU timezone and display-unit preferences across a list of IPs.

By default, it sets the PDU timezone to `America/Chicago`, sets default display preferences to Fahrenheit, feet, and PSI, and applies those same display preferences to every existing local user.

## Contents

- [What it does](#what-it-does)
- [What it does not do](#what-it-does-not-do)
- [How it works](#how-it-works)
- [Important implementation note](#important-implementation-note)
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

This script automates the PDU timezone and display-unit setup step for Raritan PX-series PDUs.

For each PDU in the IP list, it configures:

| Setting | Default target |
|---|---|
| Timezone | `America/Chicago` |
| Temperature unit | Fahrenheit / `DEG_F` |
| Length/distance unit | feet / `FEET` |
| Pressure unit | PSI |

The script changes both:

- the PDU's default display preferences
- each existing local user's display preferences, unless `--no-apply-users` is used

It is intended to replace manually logging in to each PDU to set the same timezone and display units through the web interface.

## What it does not do

- Does not change outlet power, or turn outlets on or off
- Does not reboot the PDU
- Does not update firmware
- Does not change network settings
- Does not change passwords
- Does not create or delete accounts
- Does not change roles
- Does not configure RADIUS
- Does not configure SNMP
- Does not change NTP server addresses or NTP authentication settings
- Does not change DNS settings
- Does not change thresholds

## How it works

For each PDU IP address:

1. **Log in** using the supplied username and password. The default username is `admin`.
2. **Find the desired timezone** by reading the PDU's supported timezone list. Display/id-based zones are searched first, and Olson/IANA-style zones are used as a fallback.
3. **Read current DateTime config** through raw JSON-RPC, then compare only the timezone-related `zoneCfg` portion.
4. **Set timezone if needed.** In dry-run mode it reports `would_set`; in live mode it writes the change and verifies it by reading the config again.
5. **Read and update default display preferences.** The script changes temperature, length, and pressure to Fahrenheit, feet, and PSI.
6. **Update local users by default.** Unless `--no-apply-users` is used, it loops through every account returned by `getAccountNames()` and applies the same display-unit preferences.
7. **Preserve existing user preference objects.** For each user, the script reads current preferences, copies them, changes only temperature/length/pressure, and writes the modified preference object back.
8. **Verify live writes.** Live timezone, default preference, and per-user preference changes are verified by reading them back.
9. **Continue after failures.** A failure on one PDU is reported for that IP address, and the rest of the list continues.

## Important implementation note

The script intentionally uses raw JSON-RPC for `/datetime` `getCfg` and `setCfg`.

The Raritan Python binding can fail while decoding the full typed DateTime config if a PDU response omits optional NTP-related fields such as `server1AuthKeyId` or `server2AuthKeyId`. The timezone change only needs `zoneCfg`, so the script reads the raw DateTime config as a dictionary, changes only `zoneCfg`, and writes the full raw config back with the other sub-objects preserved.

This avoids the typed DateTime/NTP decode issue while still preserving the rest of the DateTime configuration.

## Requirements

- Python 3.8+
- The `raritan` Python package: `pip install raritan`
- HTTPS access, usually TCP 443, from your machine to each PDU management interface
- Admin credentials for the target PDUs
- A `pdus.txt` file listing the PDU management IPs

## Windows setup

1. **Install Python.** Download from [python.org/downloads](https://www.python.org/downloads/) and run the installer, enabling **Add python.exe to PATH** if offered. The Python install manager through `winget install 9NQ7512CXL7T` or the Microsoft Store also works.
2. **Open PowerShell.** A normal user window is usually fine.
3. **Confirm Python works:**
   ```powershell
   py --version
   ```
   If that does not work, try `python --version`.
4. **Create a working folder** and put the script there:
   ```powershell
   mkdir C:\pdu-automation
   cd C:\pdu-automation
   ```
5. **Create `pdus.txt`**. See [The IP list file](#the-ip-list-file).
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

Default filename: `pdus.txt`, one PDU management IP per line, in the same folder you run the script from unless you pass `--ips`.

```powershell
notepad .\pdus.txt
```

Example:

```text
# Example group of Raritan PDUs
10.10.5.10
10.10.5.11  # left-side PDU
10.10.5.12

# Spare PDU
10.10.5.13
```

Blank lines are ignored. Lines starting with `#` are ignored. Inline comments after `#` are allowed. Duplicate IPs are skipped. Invalid IPs fail loudly with the line number.

## Passwords

The script needs the PDU admin password.

The password can be supplied three ways, in order of preference:

1. Typed at the interactive prompt
2. The `PDU_ADMIN_PASSWORD` environment variable
3. `--password` on the command line, supported but visible in shell history and process listings

Use an environment variable to avoid retyping the admin password:

```powershell
$env:PDU_ADMIN_PASSWORD = "your-password-here"
python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --dry-run
```

This only applies to the current PowerShell session. Close the PowerShell window when done, and avoid this while screen-sharing or recording.

## Usage

### Optional: list supported timezones

```powershell
python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --list-timezones
```

This logs in to the first PDU in `pdus.txt`, prints supported timezone entries, and exits without changing settings.

### Step 1 — dry run

```powershell
python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --dry-run -v
```

Dry-run logs in and reads current settings, but it does not write any changes.

Good dry-run output may look like this:

```text
2026-07-07 16:40:41 INFO Processing 1 PDU(s) [DRY RUN, timezone=America/Chicago, units=Fahrenheit/feet/PSI, apply_users=True, concurrency=1, verify_cert=False]...
2026-07-07 16:40:42 INFO 10.10.5.10: TIMEZONE - would_set; current=(id=17, name=(UTC-05:00) Eastern Time (US & Canada), autoDST=True); desired=(id=18, name=(UTC-06:00) Central Time (US & Canada), hasDSTInfo=True, autoDST=True)
2026-07-07 16:40:42 INFO 10.10.5.10: DEFAULT PREFS - would_set; current=(temp=DEG_C, length=METER, pressure=PASCAL); desired=(temp=DEG_F, length=FEET, pressure=PSI)
2026-07-07 16:40:42 INFO 10.10.5.10: USER admin PREFS - would_set; current=(temp=DEG_C, length=METER, pressure=PASCAL); desired=(temp=DEG_F, length=FEET, pressure=PSI)
2026-07-07 16:40:42 INFO Done. OK=1 Failed=0
```

The exact date, current timezone label, current units, and local user list will vary. `already_configured` is also a good result when the setting is already correct.

### Step 2 — live run

After dry-run succeeds:

```powershell
python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --log-file .\pdu-timezone-units.log
```

Good live output may look like this:

```text
2026-07-07 16:45:12 INFO Processing 1 PDU(s) [LIVE, timezone=America/Chicago, units=Fahrenheit/feet/PSI, apply_users=True, concurrency=1, verify_cert=False]...
2026-07-07 16:45:13 INFO 10.10.5.10: TIMEZONE - set; current=(id=17, name=(UTC-05:00) Eastern Time (US & Canada), autoDST=True); desired=(id=18, name=(UTC-06:00) Central Time (US & Canada), hasDSTInfo=True, autoDST=True)
2026-07-07 16:45:13 INFO 10.10.5.10: DEFAULT PREFS - set; current=(temp=DEG_C, length=METER, pressure=PASCAL); desired=(temp=DEG_F, length=FEET, pressure=PSI)
2026-07-07 16:45:14 INFO 10.10.5.10: USER admin PREFS - set; current=(temp=DEG_C, length=METER, pressure=PASCAL); desired=(temp=DEG_F, length=FEET, pressure=PSI)
2026-07-07 16:45:14 INFO Done. OK=1 Failed=0
```

### Rerun safely

The script is designed to be rerunnable. If a setting is already correct, it reports `already_configured`.

### Other common commands

```powershell
# Dry-run with the default pdus.txt
python .\bootstrap_pdu_timezone_units.py --dry-run -v

# Different IP list file
python .\bootstrap_pdu_timezone_units.py --ips .\my-pdus.txt --dry-run

# Longer timeout
python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --timeout 30 --dry-run

# Process four PDUs in parallel after testing
python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --concurrency 4 --dry-run

# Set only default preferences, not existing users
python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --no-apply-users --dry-run -v

# Use a different timezone
python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --timezone US/Central --dry-run -v

# Avoid automatic DST
python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --no-auto-dst --dry-run -v
```

## Command-line options

| Flag | Default | Description |
|---|---|---|
| `--ips PATH` | `pdus.txt` | IP list file |
| `--username USERNAME` | `admin` | PDU login username |
| `--password PASSWORD` | env/prompt | PDU admin password. Supported but not recommended on the CLI |
| `--timezone TIMEZONE` | `America/Chicago` | Timezone to apply. Aliases `CST`, `CDT`, `Central`, `Central Time`, and `US/Central` resolve toward `America/Chicago` |
| `--no-auto-dst` | off | Do not enable automatic DST adjustment even if the selected timezone supports it |
| `--apply-users` | on | Apply Fahrenheit/feet/PSI preferences to every existing local user |
| `--no-apply-users` | off | Only set default Fahrenheit/feet/PSI preferences; do not update existing users |
| `--list-timezones` | off | List supported timezones from the first PDU and exit without changes |
| `--timeout SECONDS` | `10` | Connection timeout per PDU |
| `--concurrency NUMBER` | `1` | Number of PDUs processed in parallel. Invalid values like `0` or negative numbers are blocked |
| `--dry-run` | off | Show what would change without writing settings |
| `-v`, `--verbose` | off | Debug logging |
| `--log-file PATH` | none | Also write logs to this file |
| `--no-insecure` | — | Require valid, trusted HTTPS certificates |
| `--insecure` | on by default | Disable HTTPS certificate verification |

## Common results

| Result | Meaning |
|---|---|
| `TIMEZONE - already_configured` | Timezone already matches the desired timezone and automatic DST setting |
| `TIMEZONE - would_set` | Dry-run found that the timezone would be changed |
| `TIMEZONE - set` | Timezone changed and verified |
| `TIMEZONE - setCfg_failed_ret_1` | The PDU rejected the DateTime config |
| `TIMEZONE - verify_failed` | The read-back verification did not match the desired timezone |
| `DEFAULT PREFS - already_configured` | Default display preferences already use Fahrenheit, feet, and PSI |
| `DEFAULT PREFS - would_set` | Dry-run found that default display preferences would change |
| `DEFAULT PREFS - set` | Default display preferences changed and verified |
| `USER <username> PREFS - already_configured` | That user's display preferences already use Fahrenheit, feet, and PSI |
| `USER <username> PREFS - would_set` | Dry-run found that the user's display preferences would change |
| `USER <username> PREFS - set` | User display preferences changed and verified |
| `USER <username> PREFS - skipped_not_modifyable` | The PDU reported that the user's preferences cannot be modified |
| `ERROR - HTTP Error 401 / 403` | Username or password is probably wrong, or the account lacks API access |
| `ERROR` connection timeout/refused/DNS failure | The script could not reach the PDU |
| `ERROR - timezone not found` | Requested timezone was not found; use `--list-timezones` |

## Troubleshooting

| Problem | Fix |
|---|---|
| `No IPs found in pdus.txt` | Make sure `pdus.txt` exists in the folder you're running from, with one IP per line |
| `invalid IP address` | Check the line number shown in the error. The file should contain one IP address per line, with optional comments after `#` |
| `python is not recognized` / `py is not recognized` | Python is not installed correctly or is not on PATH |
| `No module named raritan` | Activate your virtual environment and run `python -m pip install raritan` |
| `HTTP Error 401` or `403` | The admin password is probably wrong for that PDU, or the account does not have permission |
| Connection timed out / refused / no route to host | Check the IP, that the PDU is online, that your network can reach it, and firewall rules for TCP 443 |
| `timezone not found on this PDU` | Run `python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --list-timezones`, then rerun with a supported `--timezone` value |
| `server1AuthKeyId` | Make sure you are using the current version of this script; it uses raw JSON-RPC for `/datetime` get/set to avoid the typed DateTime/NTP decode issue |
| Some PDUs changed, others failed | Review the failed IPs printed at the end, fix the issue, and rerun. Already-correct PDUs should report `already_configured` |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All requested devices completed successfully |
| `1` | Local setup/input problem before device processing |
| `2` | One or more devices failed |
| `130` | Interrupted with Ctrl+C |

## Safety notes

- Run `--dry-run` first, every time, especially against an unfamiliar batch of PDUs.
- Keep `--concurrency 1` for first use.
- Keep `pdus.txt` limited to the intended PDUs.
- Never paste real passwords into tickets, chat, email, or screenshots.
- Prefer the interactive password prompt over `--password` on the CLI.
- Close PowerShell after using `$env:PDU_ADMIN_PASSWORD`.
- Review log files before sharing them. They may contain PDU IPs, usernames, current unit settings, desired unit settings, and timezone labels.

## Source references

- [Python for Windows documentation](https://docs.python.org/3/using/windows.html)
- [Python downloads](https://www.python.org/downloads/)
- [Python install manager release page](https://www.python.org/downloads/release/pymanager-262/)
- [Raritan Python package on PyPI](https://pypi.org/project/raritan/)
