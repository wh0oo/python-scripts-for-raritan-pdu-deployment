# bootstrap_pdu_radius.py

Configures Raritan PDU RADIUS server entries and supporting local user accounts from a shared JSON config file.

## Contents

- [What it does](#what-it-does)
- [What it does not do](#what-it-does-not-do)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Windows setup](#windows-setup)
- [The IP list file](#the-ip-list-file)
- [The RADIUS config file](#the-radius-config-file)
- [Secrets](#secrets)
- [Usage](#usage)
- [Command-line options](#command-line-options)
- [Common results](#common-results)
- [Troubleshooting](#troubleshooting)
- [Exit codes](#exit-codes)
- [Implementation notes](#implementation-notes)
- [Safety notes](#safety-notes)
- [Source references](#source-references)

## What it does

This script stages RADIUS-related configuration on Raritan PDUs.

It can do two related jobs:

1. Configure the PDU's RADIUS server entries.
2. Create local proxy/supporting user accounts with SNMPv3 settings.

The script reads a list of PDU management IPs from `pdus.txt`, reads RADIUS/user settings from a JSON config file, logs in to each PDU, and applies the requested configuration.

The default config file is:

```text
radius_config.json
```

The script is intended to be used before a later authentication-policy change. It prepares the PDUs by staging the RADIUS servers and local accounts, but it does not turn RADIUS login on by itself.

## What it does not do

- Does not change the active authentication method
- Does not change authentication order or login policy
- Does not switch the PDU from local login to RADIUS login
- Does not disable local login
- Does not change the admin password
- Does not update firmware
- Does not turn outlets on or off
- Does not reboot the PDU
- Does not change network settings
- Does not configure DNS, NTP, syslog, outlet settings, sensors, or power
- Does not delete users
- Does not modify existing users listed in the config
- Does not store secrets in the JSON config file
- Does not verify that RADIUS logins work after a future policy switch

This script makes real changes in live mode, but those changes are limited to RADIUS server entries and local user creation.

## How it works

For each PDU IP address:

1. **Read the IP list.** The script reads `pdus.txt` by default, or another file if you pass `--ips`.

2. **Read and validate the JSON config.** The script validates the requested `radius_servers` and/or `users` before contacting any PDU.

3. **Reject bad local input early.** Examples include missing config sections, duplicate RADIUS servers, duplicate usernames, invalid IP syntax in `pdus.txt`, or both skip flags being used.

4. **Prompt for the PDU admin password.** Dry-run still needs this because it logs in and reads current PDU state.

5. **In live mode, collect write-side secrets if needed.** If RADIUS servers are enabled, the script needs the RADIUS shared secret. If users are enabled, it needs the new local-user password.

6. **Log in to each PDU.** The default PDU username is `admin`.

7. **Configure RADIUS servers if enabled.** In dry-run mode, the script reports that it would set the RADIUS server list. In live mode, it writes the server list and verifies the visible fields afterward.

8. **Create local users if enabled.** The script checks existing user accounts first. If a configured user already exists, it reports `already_exists`. If not, dry-run reports `would_create`, and live mode creates the user.

9. **Verify writes where possible.** After writing RADIUS server entries, the script reads back the visible fields. After creating a user, it confirms the username appears in the account list.

10. **Report per-PDU results.** A failure on one PDU does not hide results from the rest of the batch.

## Requirements

- Python 3.8+
- The `raritan` Python package: `pip install raritan`
- HTTPS access, usually TCP 443, from your machine to each PDU management interface
- A PDU username and password with permission to configure RADIUS and create users
- A `pdus.txt` file listing the PDU management IPs
- A JSON config file such as `radius_config.json`
- The RADIUS shared secret for live RADIUS configuration
- The desired password for new local users, if creating users

## Windows setup

1. **Install Python.** Download from [python.org/downloads](https://www.python.org/downloads/) and run the installer, enabling **Add python.exe to PATH** if offered. Alternatively, the Python install manager via `winget install 9NQ7512CXL7T` or the Microsoft Store also works.

2. **Open PowerShell.** A normal user window is fine; you do not usually need Administrator.

3. **Confirm Python works:**

   ```powershell
   py --version
   ```

   If that does not work, try:

   ```powershell
   python --version
   ```

4. **Create a working folder** and put the script there:

   ```powershell
   mkdir C:\pdu-radius
   cd C:\pdu-radius
   ```

5. **Create `pdus.txt`**. See [The IP list file](#the-ip-list-file).

6. **Create `radius_config.json`**. See [The RADIUS config file](#the-radius-config-file).

7. **Create and activate a virtual environment:**

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

8. **Install the Raritan package:**

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
# Example group of PDUs
10.10.5.130
10.10.5.131
10.10.5.132

# Spare PDU
10.10.5.133
```

Inline comments are also supported:

```text
10.10.5.130  # pdu-row3-a12.example.com
10.10.5.131  # pdu-row3-a13.example.com
```

Blank lines are ignored. Lines starting with `#` are ignored. Duplicate IPs are skipped automatically.

Invalid IP addresses fail clearly and include the line number.

## The RADIUS config file

Default filename:

```text
radius_config.json
```

Create it with:

```powershell
notepad .\radius_config.json
```

Example:

```json
{
  "radius_servers": [
    {
      "server": "10.10.5.10",
      "auth_port": 1812,
      "acct_port": 1813,
      "auth_type": "PAP",
      "timeout": 2,
      "retries": 3
    },
    {
      "server": "10.10.5.11",
      "auth_port": 1812,
      "acct_port": 1813,
      "auth_type": "PAP",
      "timeout": 2,
      "retries": 3
    }
  ],
  "users": [
    {
      "username": "SVC-RADIUS-ADMIN",
      "full_name": "RADIUS service account (admin)",
      "role": "Admin"
    },
    {
      "username": "SVC-RADIUS-OPERATOR",
      "full_name": "RADIUS service account (operator)",
      "role": "Operator"
    }
  ]
}
```

Do not put passwords or shared secrets in this file.

The config can contain:

- `radius_servers` only
- `users` only
- both `radius_servers` and `users`

If both sections exist, the script manages both unless you use `--skip-radius` or `--skip-users`.

Supported RADIUS auth types:

| Value |
|---|
| `PAP` |
| `CHAP` |
| `MSCHAPV2` |

The script rejects duplicate RADIUS server entries and duplicate usernames before contacting any PDU.

## Secrets

The script can use three secrets:

| Secret | Environment variable | CLI option |
|---|---|---|
| PDU admin password | `PDU_ADMIN_PASSWORD` | `--password` |
| RADIUS shared secret | `RADIUS_SHARED_SECRET` | `--radius-secret` |
| New local-user password | `PDU_RADIUS_USER_PASSWORD` | `--user-password` |

Passing secrets on the command line is supported, but not recommended, because command-line arguments may be visible in shell history and process listings.

Prefer interactive prompts or environment variables.

### Dry-run secret behavior

Dry-run still needs the PDU admin password because it logs in and reads current state.

Dry-run does **not** ask for the RADIUS shared secret.

Dry-run does **not** ask for the new local-user password.

That is intentional. Dry-run does not write RADIUS settings and does not create users.

### Live secret behavior

In live mode, if the RADIUS shared secret is typed interactively, the script asks for it twice:

```text
RADIUS shared secret
Confirm RADIUS shared secret
```

If the new local-user password is typed interactively, the script asks for it twice:

```text
New user account password
Confirm new user account password
```

Values supplied by environment variable or CLI option are trusted as-is.

### Environment variable example

```powershell
$env:PDU_ADMIN_PASSWORD = "<pdu-admin-password>"
$env:RADIUS_SHARED_SECRET = "<radius-shared-secret>"
$env:PDU_RADIUS_USER_PASSWORD = "<new-local-user-password>"
```

These environment variables only apply to the current PowerShell session. Close the window when done, and avoid this while screen-sharing or recording.

## Usage

### Step 1 — dry run

Run dry-run first:

```powershell
python .\bootstrap_pdu_radius.py --ips .\pdus.txt --config .\radius_config.json --dry-run -v
```

Good dry-run output:

```text
2026-07-05 10:12:04 INFO Processing 1 PDU(s) [DRY RUN, concurrency=1, verify_cert=False]...
2026-07-05 10:12:05 INFO 10.10.5.130: RADIUS - would_set; servers=2
2026-07-05 10:12:05 INFO 10.10.5.130: USER SVC-RADIUS-ADMIN - would_create
2026-07-05 10:12:05 INFO 10.10.5.130: USER SVC-RADIUS-OPERATOR - would_create
2026-07-05 10:12:05 INFO Done. OK=1 Failed=0
```

The important dry-run results are:

```text
RADIUS - would_set
USER <username> - would_create
USER <username> - already_exists
```

If visible RADIUS fields already match, dry-run may include this note:

```text
note=visible fields already match; secret cannot be verified and will still be reapplied
```

That is expected. The RADIUS shared secret cannot be verified by reading it back, so a future live run will still reapply the RADIUS server settings.

### Step 2 — live run

After the dry-run output looks correct:

```powershell
python .\bootstrap_pdu_radius.py --ips .\pdus.txt --config .\radius_config.json -v --log-file .\pdu-radius.log
```

Good live output:

```text
2026-07-05 10:20:04 INFO Processing 1 PDU(s) [LIVE, concurrency=1, verify_cert=False]...
2026-07-05 10:20:05 INFO 10.10.5.130: RADIUS - set; servers=2
2026-07-05 10:20:06 INFO 10.10.5.130: USER SVC-RADIUS-ADMIN - created
2026-07-05 10:20:07 INFO 10.10.5.130: USER SVC-RADIUS-OPERATOR - created
2026-07-05 10:20:07 INFO Done. OK=1 Failed=0
```

The important live results are:

```text
RADIUS - set
USER <username> - created
USER <username> - already_exists
```

### Step 3 — confirm with another dry run

After the live run, run dry-run again:

```powershell
python .\bootstrap_pdu_radius.py --ips .\pdus.txt --config .\radius_config.json --dry-run -v
```

Expected user result after a successful live run:

```text
USER <username> - already_exists
```

Expected RADIUS result:

```text
RADIUS - would_set
```

This may seem odd at first, but it is expected. The script cannot verify the RADIUS shared secret by reading it back from the API, so it treats a future live run as something that would reapply the RADIUS server settings.

### Skip modes

Only manage local users and leave RADIUS server entries alone:

```powershell
python .\bootstrap_pdu_radius.py --ips .\pdus.txt --config .\radius_config.json --skip-radius --dry-run -v
```

Only manage RADIUS server entries and leave users alone:

```powershell
python .\bootstrap_pdu_radius.py --ips .\pdus.txt --config .\radius_config.json --skip-users --dry-run -v
```

This is invalid:

```powershell
python .\bootstrap_pdu_radius.py --ips .\pdus.txt --config .\radius_config.json --skip-radius --skip-users
```

The script rejects it because there is nothing left to do.

### Recommended rollout process

1. Put one test PDU in `pdus.txt`.
2. Confirm `radius_config.json` has the intended RADIUS servers and users.
3. Dry-run the one PDU.
4. If dry-run looks good, run live against the one PDU.
5. Dry-run again and confirm local users report `already_exists`.
6. Check the PDU directly if needed.
7. Add the remaining PDU IPs to `pdus.txt`.
8. Dry-run again.
9. Run live against the remaining PDUs.
10. Save the log file for your records.

### Other common commands

```powershell
# Dry-run with the default pdus.txt and radius_config.json
python .\bootstrap_pdu_radius.py --ips .\pdus.txt --config .\radius_config.json --dry-run -v

# Live run with a log file
python .\bootstrap_pdu_radius.py --ips .\pdus.txt --config .\radius_config.json -v --log-file .\pdu-radius.log

# Different IP list file
python .\bootstrap_pdu_radius.py --ips .\my-pdus.txt --config .\radius_config.json --dry-run -v

# Different config file
python .\bootstrap_pdu_radius.py --ips .\pdus.txt --config .\radius_config.example.json --dry-run -v

# Different PDU username
python .\bootstrap_pdu_radius.py --ips .\pdus.txt --config .\radius_config.json --username admin --dry-run -v

# Longer timeout
python .\bootstrap_pdu_radius.py --ips .\pdus.txt --config .\radius_config.json --dry-run --timeout 30
```

Keep `--concurrency 1`, the default, for first use.

## Command-line options

| Flag | Default | Description |
|---|---|---|
| `--ips PATH` | `pdus.txt` | IP list file |
| `--config PATH` | `radius_config.json` | JSON config file describing `radius_servers` and/or `users` |
| `--username USERNAME` | `admin` | PDU login username |
| `--password PASSWORD` | *(env/prompt)* | PDU admin password. Supported but not recommended on the CLI |
| `--radius-secret SECRET` | *(env/prompt)* | RADIUS shared secret. Supported but not recommended on the CLI |
| `--user-password PASSWORD` | *(env/prompt)* | Password for every new local user in the config. Supported but not recommended on the CLI |
| `--skip-radius` | off | Do not touch RADIUS server configuration |
| `--skip-users` | off | Do not create any local user accounts |
| `--timeout SECONDS` | `10` | Connection timeout for ordinary API calls, per PDU |
| `--concurrency NUMBER` | `1` | Number of PDUs processed in parallel. Invalid values like `0` or negative numbers are blocked |
| `--dry-run` | off | Show what would happen, but do not write RADIUS settings or create users |
| `-v`, `--verbose` | off | Debug logging |
| `--log-file PATH` | none | Also write logs to this file |
| `--no-insecure` | — | Require valid, trusted HTTPS certificates |
| `--insecure` | on by default | Disable HTTPS certificate verification. This is already the default, since many PDUs use self-signed certs |

## Common results

| Result | Meaning |
|---|---|
| `RADIUS - would_set` | Dry-run mode. The script would write the configured RADIUS server entries during a live run |
| `RADIUS - set` | Live mode. The script wrote the configured RADIUS server entries and verified visible fields afterward |
| `RADIUS - setRadiusServers_failed_ret_<N>` | The PDU rejected the RADIUS server settings |
| `RADIUS - verify_failed` | RADIUS settings were written, but the visible fields read afterward did not match the desired values |
| `USER <username> - already_exists` | The account already exists. The script did not modify it |
| `USER <username> - would_create` | Dry-run mode. The account does not exist and would be created during live mode |
| `USER <username> - created` | Live mode. The account was created and verified afterward |
| `USER <username> - role_not_found:<role>` | The requested role name was not found on the PDU |
| `USER <username> - user_already_exists` | The create call reported that the user already exists |
| `USER <username> - password_too_short` | The new local-user password is shorter than the PDU allows |
| `USER <username> - password_too_long` | The new local-user password is longer than the PDU allows |
| `USER <username> - password_empty` | The new local-user password was rejected as empty |
| `USER <username> - password_has_control_chars` | The new local-user password contains disallowed control characters |
| `USER <username> - password_needs_lowercase` | The new local-user password needs a lowercase letter |
| `USER <username> - password_needs_uppercase` | The new local-user password needs an uppercase letter |
| `USER <username> - password_needs_numeric` | The new local-user password needs a numeric character |
| `USER <username> - password_needs_special` | The new local-user password needs a special character |
| `USER <username> - password_too_short_for_snmp` | The password is too short for the PDU's SNMP requirements |
| `USER <username> - username_invalid` | The PDU rejected the username |
| `USER <username> - verify_failed_not_found_after_create` | The create call returned success, but the username was not found afterward |
| `ERROR - HTTP Error 401 / 403` | The username or password is wrong, or the account does not have permission |
| `ERROR` *(connection timeout / refused / DNS failure)* | The script could not reach the PDU |

## Troubleshooting

| Problem | Fix |
|---|---|
| `No IPs found in pdus.txt` | Make sure `pdus.txt` exists in the folder you are running from, with one IP per line |
| `invalid IP address` | Check `pdus.txt` for a typo. The script reports the line number |
| `no radius_servers defined` | Add a `radius_servers` section to `radius_config.json`, or use `--skip-radius` if you only want to manage users |
| `no users defined` | Add a `users` section to `radius_config.json`, or use `--skip-users` if you only want to manage RADIUS servers |
| `Both --skip-radius and --skip-users were given` | Remove one of the skip options. Using both leaves nothing to do |
| `unknown auth_type` | Use one of `PAP`, `CHAP`, or `MSCHAPV2` |
| `duplicate radius server` | Remove the duplicate server from `radius_config.json` |
| `duplicate username` | Remove the duplicate username from `radius_config.json` |
| `RADIUS shared secret and confirmation do not match` | Re-run the live command and type the same shared secret both times |
| `New user account password and confirmation do not match` | Re-run the live command and type the same new local-user password both times |
| `python is not recognized` / `py is not recognized` | Python is not installed correctly or is not on PATH — reinstall and enable the PATH option |
| `No module named raritan` | Activate your virtual environment and run `python -m pip install raritan` |
| Connection timed out / refused / no route to host | Check the IP, that the PDU is online, that your network can reach it, and firewall rules for TCP 443 |
| `HTTP Error 401` or `403` | The username or password is wrong, or the account does not have permission |
| `RADIUS - setRadiusServers_failed_ret_<N>` | The PDU rejected the RADIUS server settings. Check the config values and confirm this PDU supports the settings being sent |
| `RADIUS - verify_failed` | The script wrote RADIUS server settings, but the visible fields read afterward did not match. Check the PDU directly and review the log |
| `USER <username> - role_not_found:<role>` | The role name in `radius_config.json` does not exist on the PDU. Fix the role name or create the role separately |
| `USER <username> - password_needs_uppercase` / `_numeric` / `_special` / `too_short` | The new local-user password does not meet the PDU password policy. Choose a stronger password and rerun |
| `USER <username> - verify_failed_not_found_after_create` | The create call returned success, but the account did not appear afterward. Check the PDU directly and review the log |
| Some PDUs changed, others failed | Review the failed IPs printed at the end, fix the issue, and rerun dry-run first |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All PDUs processed successfully |
| `1` | Local setup/input problem, such as invalid IP syntax, no IPs, invalid config, both skip flags being used, invalid concurrency, or interactive secret confirmation mismatch |
| `2` | One or more PDUs failed |
| `130` | Interrupted with Ctrl+C |

## Implementation notes

The script uses the Raritan Python package and the Xerus JSON-RPC API.

For login verification, it uses a small authenticated user API call:

```python
usermgmt.User(...).getInfo()
```

It intentionally does not use:

```python
pdumodel.Pdu(...).getMetaData()
```

because that call can fail on some tested PDU/API/library combinations while decoding unrelated fields.

The script uses these Raritan resource IDs:

```text
/auth/radius            RADIUS manager
/auth/role              Role manager
/auth/user              User manager
/auth/user/<username>   User login check
```

### RADIUS shared secret behavior

The RADIUS shared secret cannot be reliably read back from the API.

Because of that, live mode always calls `setRadiusServers()` when RADIUS servers are enabled for the run. It does this even if the visible fields already match.

This avoids a false success case where the server IPs, ports, auth type, timeout, and retries match, but the shared secret is wrong or stale.

### Local user behavior

The script creates configured local users if they do not already exist.

If a user already exists, the script reports `already_exists` and does not modify that user.

If a user does not exist, dry-run reports `would_create`.

In live mode, the script creates the user and then verifies that the account name appears afterward.

### Role behavior

If a configured user specifies a role, the script resolves the role name through the PDU role list.

Role lookup happens before dry-run reports `would_create`, so typoed role names should be caught in dry-run.

### SNMPv3 behavior

When creating users, the script enables SNMPv3 settings.

Current defaults:

| Setting | Value |
|---|---|
| Security level | `AUTH_PRIV` |
| Authentication protocol | `SHA1` |
| Privacy protocol | `AES128` |
| Authentication passphrase | Same as user password |
| Privacy passphrase | Same as authentication passphrase |

These defaults are kept in the script rather than exposed as command-line flags.

## Safety notes

- Run `--dry-run` first, every time, especially against an unfamiliar batch of PDUs.
- Keep `--concurrency 1` for first use.
- Use one test PDU first.
- Keep `pdus.txt` limited to the intended PDUs.
- Keep `radius_config.json` limited to the intended RADIUS servers and users.
- Remember that this script makes real changes in live mode.
- Remember that this script does not enable RADIUS login by itself.
- Do not put passwords or shared secrets in `radius_config.json`.
- Do not paste real passwords or shared secrets into tickets, chat, email, or screenshots.
- Prefer interactive prompts or environment variables over CLI secret arguments.
- Close PowerShell after using secret environment variables.
- Save a log file for audit records.
- Review log files before sharing them. They should not contain passwords or shared secrets, but they may contain device IPs, usernames, RADIUS server IPs, and other infrastructure-identifying details.

## Source references

- [Python for Windows documentation](https://docs.python.org/3/using/windows.html)
- [Python downloads](https://www.python.org/downloads/)
- [Python install manager release page](https://www.python.org/downloads/release/pymanager-262/)
- [Raritan Python package on PyPI](https://pypi.org/project/raritan/)
- [Raritan / Server Technology JSON-RPC API reference](https://help.servertech.com/json-rpc/4.0.21/)
