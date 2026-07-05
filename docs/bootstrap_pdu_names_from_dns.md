# bootstrap_pdu_names_from_dns.py

Sets each Raritan PDU's user-defined name from reverse DNS.

## Contents

- [What it does](#what-it-does)
- [What it does not do](#what-it-does-not-do)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Windows setup](#windows-setup)
- [The IP list file](#the-ip-list-file)
- [Reverse DNS](#reverse-dns)
- [Passwords](#passwords)
- [Usage](#usage)
- [Command-line options](#command-line-options)
- [Common results](#common-results)
- [Troubleshooting](#troubleshooting)
- [Exit codes](#exit-codes)
- [Implementation notes](#implementation-notes)
- [Safety notes](#safety-notes)
- [Source references](#source-references)

## What it does

This script sets the user-defined name on each Raritan PDU based on reverse DNS.

It reads a list of PDU management IP addresses, performs a reverse DNS lookup for each IP, takes the first hostname label, uppercases it, and sets the PDU name to that value.

Generic example:

```text
10.10.5.130 -> pdu-row3-a12.example.com -> PDU-ROW3-A12
```

This is useful when DNS is already the source of truth for how PDUs should be named. Instead of manually logging in to each PDU and typing the name, the script derives the name from DNS and applies it consistently.

The only PDU setting it changes:

| | |
|---|---|
| Setting | User-defined PDU name |
| Source | Reverse DNS / PTR record |
| Format | First hostname label, uppercased |

Example:

| Reverse DNS | PDU name set |
|---|---|
| `pdu-row3-a12.example.com` | `PDU-ROW3-A12` |

## What it does not do

- Does not change the admin password
- Does not update firmware
- Does not turn outlets on or off
- Does not reboot the PDU
- Does not change network settings
- Does not configure SNMP, syslog, NTP, DNS, users, roles, outlets, or sensors
- Does not create or delete accounts
- Does not create or modify DNS records
- Does not change hostnames
- Does not guess names when reverse DNS is missing or unclear

This script only reads DNS and uses that result to set the PDU's own name field.

## How it works

For each PDU IP address:

1. **Read the IP list.** The script reads `pdus.txt` by default, or another file if you pass `--ips`.

2. **Preflight reverse DNS before logging in to any PDU.** The script resolves reverse DNS for every IP first.

3. **Derive the target PDU name.** It takes the first hostname label and uppercases it.

   Example:

   ```text
   pdu-row3-a12.example.com -> PDU-ROW3-A12
   ```

4. **Validate the derived name.** The target name must be usable as a host-style name.

5. **Check for duplicate target names.** If two or more IPs produce the same target name, all of the colliding IPs are skipped and reported as failed.

6. **Prompt for the PDU password only if there is something safe to process.** If no PDUs pass DNS preflight, the script exits without asking for the password.

7. **Log in to each PDU that passed preflight.** By default, it logs in as `admin`.

8. **Read the current PDU name.**

9. **Compare the current name to the DNS-derived target name.**

   If the current name already matches, the script reports:

   ```text
   OK - already_named
   ```

10. **In dry-run mode, stop before making changes.**

    If the name does not match and `--dry-run` was used, the script reports:

    ```text
    OK - would_change
    ```

11. **In live mode, set the PDU name.**

    If the current name does not match the target name, the script updates the PDU name.

12. **Verify the change.**

    After setting the name, the script reads the PDU name again and confirms it matches the target.

13. **Report the result and move to the next PDU.**

    A failure on one PDU does not stop every other valid PDU from being processed.

## Requirements

- Python 3.8+
- The `raritan` Python package: `pip install raritan`
- HTTPS access, usually TCP 443, from your machine to each PDU's management interface
- A PDU username and password with permission to change the PDU name
- A `pdus.txt` file listing the PDU management IPs
- Working reverse DNS / PTR records for the PDU IPs

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

   At least one should succeed.

4. **Create a working folder** and put the script there:

   ```powershell
   mkdir C:\pdu-names
   cd C:\pdu-names
   ```

5. **Create `pdus.txt`**. See [The IP list file](#the-ip-list-file) below.

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

## Reverse DNS

This script depends on reverse DNS.

Every PDU IP should have a PTR record that resolves to the hostname that should become the PDU name.

Example:

```text
10.10.5.130 -> pdu-row3-a12.example.com
```

The script uses only the first label:

```text
pdu-row3-a12
```

Then uppercases it:

```text
PDU-ROW3-A12
```

Before running the script, you can check reverse DNS from PowerShell:

```powershell
Resolve-DnsName 10.10.5.130 -Type PTR
```

If reverse DNS is wrong, fix DNS first. The script does not create or update DNS records.

### Missing reverse DNS

If an IP has no usable reverse DNS, that IP is marked as failed and skipped.

The script still processes the other IPs that passed preflight.

### Duplicate name protection

Before logging in to any PDU, the script checks for duplicate target names.

If two IP addresses produce the same target PDU name, all of the IPs involved in that collision are skipped and reported as failed.

Example:

```text
10.10.5.130 -> pdu-row3-a12.example.com -> PDU-ROW3-A12
10.10.5.131 -> pdu-row3-a12.example.com -> PDU-ROW3-A12
```

Both IPs are skipped.

The script does this because it cannot safely know which PDU should get that name. It is safer to report the DNS problem and make no change to the affected devices.

## Passwords

The script needs the current PDU password for the selected username.

The default username is:

```text
admin
```

The password can be supplied three ways, in order of preference:

1. Typed at an interactive prompt
2. The `PDU_ADMIN_PASSWORD` environment variable
3. `--password` on the command line

The safest normal method is to type it when prompted.

Passing a password on the command line is supported, but it is less safe because command-line arguments can be visible in shell history and process listings.

The script does not print the password while you type it.

Important behavior: the script performs DNS preflight before asking for the password. If no PDUs pass DNS preflight, the script does not ask for the password because there is nothing safe to process.

**Use an environment variable to avoid retyping the PDU password:**

```powershell
$env:PDU_ADMIN_PASSWORD = "your-current-password"
python .\bootstrap_pdu_names_from_dns.py --ips .\pdus.txt --dry-run -v
```

This only applies to the current PowerShell session. Close the window when done, and avoid this while screen-sharing or recording.

## Usage

### Step 1 — confirm reverse DNS

Before changing anything, spot-check one or more IPs:

```powershell
Resolve-DnsName 10.10.5.130 -Type PTR
```

Expected idea:

```text
10.10.5.130 -> pdu-row3-a12.example.com
```

The resulting PDU name would be:

```text
PDU-ROW3-A12
```

### Step 2 — dry run

Run the script in dry-run mode first:

```powershell
python .\bootstrap_pdu_names_from_dns.py --ips .\pdus.txt --dry-run -v
```

The script preflights reverse DNS first. If at least one PDU passes DNS preflight, it prompts for the PDU password.

Good dry-run output:

```text
2026-07-05 10:12:04 INFO Preflighting reverse DNS for 2 PDU(s) [DRY RUN]...
2026-07-05 10:12:04 INFO 10.10.5.130: reverse DNS=pdu-row3-a12.example.com; target_name=PDU-ROW3-A12
2026-07-05 10:12:04 INFO 10.10.5.131: reverse DNS=pdu-row3-a13.example.com; target_name=PDU-ROW3-A13
2026-07-05 10:12:08 INFO Processing 2 PDU(s) [DRY RUN, concurrency=1, verify_cert=False]...
2026-07-05 10:12:09 INFO 10.10.5.130: OK - already_named; reverse DNS=pdu-row3-a12.example.com; current="PDU-ROW3-A12"
2026-07-05 10:12:10 INFO 10.10.5.131: OK - would_change; reverse DNS=pdu-row3-a13.example.com; current=""; target="PDU-ROW3-A13"
2026-07-05 10:12:10 INFO Done. OK=2 Failed=0
```

The exact date, time, names, and current values will be different.

The important dry-run results are:

```text
OK - already_named
OK - would_change
```

`already_named` means the PDU already matches DNS.

`would_change` means the PDU does not match DNS, and the script would set it during a live run.

### Step 3 — live run

Once the dry-run output looks correct:

```powershell
python .\bootstrap_pdu_names_from_dns.py --ips .\pdus.txt -v --log-file .\pdu-names.log
```

Good live output:

```text
2026-07-05 10:20:04 INFO Preflighting reverse DNS for 2 PDU(s) [LIVE]...
2026-07-05 10:20:04 INFO 10.10.5.130: reverse DNS=pdu-row3-a12.example.com; target_name=PDU-ROW3-A12
2026-07-05 10:20:04 INFO 10.10.5.131: reverse DNS=pdu-row3-a13.example.com; target_name=PDU-ROW3-A13
2026-07-05 10:20:08 INFO Processing 2 PDU(s) [LIVE, concurrency=1, verify_cert=False]...
2026-07-05 10:20:09 INFO 10.10.5.130: OK - already_named; reverse DNS=pdu-row3-a12.example.com; current="PDU-ROW3-A12"
2026-07-05 10:20:10 INFO 10.10.5.131: OK - changed; reverse DNS=pdu-row3-a13.example.com; old=""; new="PDU-ROW3-A13"
2026-07-05 10:20:10 INFO Done. OK=2 Failed=0
```

The line to look for:

```text
<IP>: OK - changed
```

That means the script set the PDU name and verified it afterward.

### Step 4 — confirm with another dry run

After the live run, run dry-run again:

```powershell
python .\bootstrap_pdu_names_from_dns.py --ips .\pdus.txt --dry-run -v
```

The expected final state is:

```text
OK - already_named
```

for every PDU that was successfully changed.

### Recommended rollout process

1. Put one test PDU in `pdus.txt`.
2. Confirm reverse DNS for that IP.
3. Run the script with `--dry-run`.
4. If the output looks correct, run it live.
5. Run dry-run again and confirm it reports `already_named`.
6. Add the remaining PDU IPs to `pdus.txt`.
7. Confirm reverse DNS for the full list.
8. Dry-run again.
9. Run live.
10. Save the log file for your records.

### Other common commands

```powershell
# Dry-run with the default pdus.txt
python .\bootstrap_pdu_names_from_dns.py --ips .\pdus.txt --dry-run -v

# Live run with the default pdus.txt
python .\bootstrap_pdu_names_from_dns.py --ips .\pdus.txt -v --log-file .\pdu-names.log

# Different IP list file
python .\bootstrap_pdu_names_from_dns.py --ips .\my-pdus.txt --dry-run -v

# Different username
python .\bootstrap_pdu_names_from_dns.py --ips .\pdus.txt --username admin --dry-run -v

# Longer timeout
python .\bootstrap_pdu_names_from_dns.py --ips .\pdus.txt --dry-run --timeout 30
```

Keep `--concurrency 1`, the default, for first use.

## Command-line options

| Flag | Default | Description |
|---|---|---|
| `--ips PATH` | `pdus.txt` | IP list file |
| `--username USERNAME` | `admin` | PDU username |
| `--password PASSWORD` | *(env/prompt)* | PDU password. Supported but not recommended on the CLI — can appear in shell history |
| `--timeout SECONDS` | `10` | Connection timeout for ordinary API calls, per PDU |
| `--concurrency NUMBER` | `1` | Number of PDUs processed in parallel. Invalid values like `0` or negative numbers are blocked |
| `--dry-run` | off | Show what would change, but do not set PDU names |
| `-v`, `--verbose` | off | Debug logging |
| `--log-file PATH` | none | Also write logs to this file |
| `--no-insecure` | — | Require valid, trusted HTTPS certificates |
| `--insecure` | on by default | Disable HTTPS certificate verification. This is already the default, since many PDUs use self-signed certs |

## Common results

| Result | Meaning |
|---|---|
| `OK - already_named` | The PDU's current name already matches the DNS-derived target name. Nothing changed |
| `OK - would_change` | Dry-run mode. The PDU does not currently match DNS, but no change was made |
| `OK - changed` | Live mode. The PDU name was changed and verified |
| `ERROR - NO REVERSE DNS / PTR RECORD` | The IP has no usable PTR record. The PDU was not contacted |
| `ERROR - DNS lookup failed` | DNS lookup failed. The PDU was not contacted |
| `ERROR - hostname produced invalid PDU name` | Reverse DNS returned a hostname that did not produce a valid target name |
| `ERROR - duplicate target PDU name` | Two or more IPs produced the same target name. All colliding IPs were skipped |
| `ERROR - setSettings_failed_ret_1` | The PDU rejected the name change request. On tested bindings, return code `0` means OK and `1` means invalid parameters |
| `ERROR - verify_failed` | The script tried to set the name, but reading it afterward did not return the expected value |
| `ERROR - HTTP Error 401 / 403` | The username or password is wrong, or the account does not have permission |
| `ERROR` *(connection timeout / refused / DNS failure)* | The script could not reach the PDU at that IP |

## Troubleshooting

| Problem | Fix |
|---|---|
| `No IPs found in pdus.txt` | Make sure `pdus.txt` exists in the folder you are running from, with one IP per line |
| `invalid IP address` | Check `pdus.txt` for a typo. The script reports the line number |
| `python is not recognized` / `py is not recognized` | Python is not installed correctly or is not on PATH — reinstall and enable the PATH option |
| `No module named raritan` | Activate your virtual environment and run `python -m pip install raritan` |
| `NO REVERSE DNS / PTR RECORD` | The IP address does not have a PTR record. Add or fix reverse DNS, then rerun |
| `DNS lookup failed` | Check DNS resolution from the computer running the script. Confirm the IP is correct and the DNS servers can resolve the PTR record |
| `hostname produced invalid PDU name` | The hostname returned by reverse DNS produced a name the script will not use. Fix the DNS name, then rerun |
| `duplicate target PDU name` | Two or more IPs resolved to hostnames that produced the same PDU name. Fix reverse DNS so each PDU has a unique name |
| Connection timed out / refused / no route to host | Check the IP, that the PDU is online, that your network can reach it, and firewall rules for TCP 443 |
| `HTTP Error 401` or `403` | The username or password is wrong, or the account does not have permission |
| `setSettings_failed_ret_1` | The PDU rejected the setting update. Confirm the account has permission to change PDU settings, and confirm this PDU model/API supports sparse `setSettings` updates for the PDU name |
| `verify_failed` | The script attempted to set the name, but reading it afterward did not return the expected value. Check the PDU directly and review the log |
| Some PDUs changed, others failed | Review the failed IPs printed at the end, fix the DNS/network/credential/device-side issue, and rerun dry-run first |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All PDUs processed successfully |
| `1` | Local setup/input problem before device processing, such as invalid IP syntax, no IPs, or invalid concurrency |
| `2` | One or more PDUs failed, including DNS preflight failures, duplicate derived names, login failures, API failures, or verification failures |
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

For reading and setting the PDU name, the script intentionally uses raw JSON-RPC calls against:

```text
/model/pdu/0
```

It does not use the typed `pdumodel.Pdu.getSettings()` wrapper for this work. On tested hardware, the typed wrapper failed while decoding the full settings structure because the device response omitted fields such as `inletWiring`.

The script only needs the `name` field, so it reads the raw `getSettings` response directly and sends a sparse `setSettings` request containing only:

```json
{"settings": {"name": "<target-name>"}}
```

This avoids depending on a full decoded PDU settings object when only the name needs to be changed.

## Safety notes

- Run `--dry-run` first, every time, especially against an unfamiliar batch of PDUs.
- Confirm reverse DNS before the live run.
- Keep `--concurrency 1` for first use.
- Keep `pdus.txt` limited to the intended PDUs.
- Fix DNS problems before running live.
- Do not use this script to work around bad DNS. It assumes DNS is the source of truth.
- Never paste real passwords into tickets, chat, email, or screenshots.
- Prefer the interactive password prompt over `--password` on the CLI.
- Close PowerShell after using `$env:PDU_ADMIN_PASSWORD`.
- Review log files before sharing them. They should not contain passwords, but they may contain device names, IPs, and DNS names.

## Source references

- [Python for Windows documentation](https://docs.python.org/3/using/windows.html)
- [Python downloads](https://www.python.org/downloads/)
- [Python install manager release page](https://www.python.org/downloads/release/pymanager-262/)
- [Raritan Python package on PyPI](https://pypi.org/project/raritan/)
- [Raritan / Server Technology JSON-RPC API reference](https://help.servertech.com/json-rpc/4.0.21/)
