# bootstrap_pdu_firmware.py

Checks the installed firmware version, or uploads and installs a firmware image, across a list of Raritan PDU IPs.

## Contents

- [What it does](#what-it-does)
- [What it does not do](#what-it-does-not-do)
- [How --check works](#how---check-works)
- [How --update works](#how---update-works)
- [Same-version and downgrade protection](#same-version-and-downgrade-protection)
- [Requirements](#requirements)
- [Windows setup](#windows-setup)
- [The IP list file](#the-ip-list-file)
- [Password](#password)
- [Usage](#usage)
- [Command-line options](#command-line-options)
- [Common results](#common-results)
- [Troubleshooting](#troubleshooting)
- [Exit codes](#exit-codes)
- [A note on Ctrl+C during --update](#a-note-on-ctrlc-during---update)
- [Safety notes](#safety-notes)
- [Source references](#source-references)

## What it does

This script has two modes, exactly one of which must be given:

| Flag | Behavior |
|---|---|
| `--check` | Read-only. Reports the currently installed firmware version. |
| `--update` | Uploads a firmware image and installs it. |

## What it does not do

- Does not change the admin password (see `bootstrap_pdu_passwords.py`)
- Does not turn outlets on or off
- Does not change network settings
- Does not configure SNMP, syslog, NTP, DNS, users, roles, or names
- Does not create or delete accounts
- Does not automate firmware **downgrades** — see [Same-version and downgrade protection](#same-version-and-downgrade-protection)

## How --check works

For each PDU:

1. Log in as `admin`.
2. Read the currently installed firmware version.
3. Report `OK - firmware=<version>`.

Nothing is uploaded and nothing changes. `--dry-run` has no effect here (it only applies to `--update`) and the script will warn you if you combine them.

## How --update works

For each PDU:

1. **Log in** and read the current firmware version.
2. **If `--dry-run`**, stop here and report `OK - would_update; current=<version>; image=<path>` — no file is uploaded.
3. **Upload the image file** to the PDU.
4. **Read the PDU's own assessment of the image**: `version`, `valid`, `compatible`, `product`, `platform`, `min_required_version`, `min_downgrade_version`.
5. **Reject invalid or incompatible images.** If the PDU reports the image as not valid, or not compatible with this specific device, the image is discarded and the update is never started — reported as an `ERROR`.
6. **Skip same-version images by default** — see below.
7. **Reject downgrade-looking images** — see below.
8. **If the version can't be compared** from the version strings, the script proceeds anyway (logged as a warning), since the image was already confirmed valid and compatible in step 5.
9. **Start the update** and poll the PDU's firmware update status until it reports success, failure, or a timeout. The PDU reboots during this window, so temporary communication failures here are expected and only logged at debug level.
10. **After success**, wait for the management interface to respond again and confirm the new version.
11. **Report** `OK - changed; old=<version>; image_version=<version>; new=<version>`.

## Same-version and downgrade protection

The script compares the version number in the uploaded image against the currently installed version (e.g. `4.3.13` or `4.3.13-52884`):

| Comparison | Behavior |
|---|---|
| **Same version** | Skipped by default, reported as `OK - skipped_same_version`. Use `--allow-same-version` to install anyway. |
| **Image is older** (downgrade) | Always treated as a failure — there is no flag to override this. Downgrades are intentionally not automated by this script. If you need to downgrade a PDU, do it manually per Raritan's guidance. |
| **Can't be determined** | The script proceeds, since the PDU itself already confirmed the image is valid and compatible. |

## Requirements

- Python 3.8+
- The `raritan` Python package: `pip install raritan`
- HTTPS access (usually TCP 443) from your machine to each PDU's management interface
- The current admin password for the PDUs
- A `pdus.txt` file listing the PDU IPs
- For updates: the correct firmware image file (`.bin`) for your PDU model

## Windows setup

1. **Install Python.** Download from [python.org/downloads](https://www.python.org/downloads/) and run the installer, enabling **Add python.exe to PATH** if offered. (Alternatively, the Python install manager via `winget install 9NQ7512CXL7T` or the Microsoft Store also works.)
2. **Open PowerShell.** A normal user window is fine; you don't usually need Administrator.
3. **Confirm Python works:**
   ```powershell
   py --version
   ```
   If that doesn't work, try `python --version`. At least one should succeed.
4. **Create a working folder** and put the script (and firmware image, if updating) there:
   ```powershell
   mkdir C:\pdu-firmware
   cd C:\pdu-firmware
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
# Cabinet BJ16-BJ25 PDUs
10.42.98.10
10.42.98.11
10.42.98.12
```

Blank lines are ignored. Lines starting with `#` are ignored. Duplicate IPs are skipped automatically.

## Password

The script needs one password: the current `admin` password. It can be supplied three ways, in order of preference:

1. Typed at an interactive prompt (safest)
2. The `PDU_ADMIN_PASSWORD` environment variable
3. `--password` on the command line (supported, but visible in shell history — avoid unless you have a specific reason)

**Use an environment variable to avoid retyping it:**
```powershell
$env:PDU_ADMIN_PASSWORD = "your-current-password"
python .\bootstrap_pdu_firmware.py --ips .\pdus.txt --check
```
This only applies to the current PowerShell session — close the window when done, and avoid this while screen-sharing or recording.

## Usage

**Step 1 — check current firmware** (always read-only, no risk):
```powershell
python .\bootstrap_pdu_firmware.py --ips .\pdus.txt --check -v
```
```text
2026-07-04 10:12:04 INFO Processing 1 PDU(s) [CHECK, concurrency=1, verify_cert=False]...
2026-07-04 10:12:05 INFO 10.42.98.137: OK - firmware=4.3.0.5-51180
2026-07-04 10:12:05 INFO Done. OK=1 Failed=0
```

**Step 2 — dry-run the update** (no file is uploaded, nothing changes):
```powershell
python .\bootstrap_pdu_firmware.py --ips .\pdus.txt --update --image .\pdu-firmware.bin --dry-run -v
```
```text
2026-07-04 10:20:11 INFO Processing 1 PDU(s) [UPDATE DRY RUN, concurrency=1, verify_cert=False]...
2026-07-04 10:20:12 INFO 10.42.98.137: OK - would_update; current=4.3.0.5-51180; image=.\pdu-firmware.bin
2026-07-04 10:20:12 INFO Done. OK=1 Failed=0
```
The line to look for: `<IP>: OK - would_update`.

**Step 3 — live update.** This takes noticeably longer than a password change — the script uploads the image, waits for the PDU to validate it, starts the update, and polls until it reports success or failure. The PDU reboots during this, so it can take several minutes per PDU. **Don't close PowerShell or disconnect the network while this runs.**
```powershell
python .\bootstrap_pdu_firmware.py --ips .\pdus.txt --update --image .\pdu-firmware.bin -v --log-file .\pdu-firmware.log
```
```text
2026-07-04 10:30:02 INFO Processing 1 PDU(s) [UPDATE LIVE, concurrency=1, verify_cert=False]...
2026-07-04 10:30:03 INFO 10.42.98.137: current firmware=4.3.0.5-51180; uploading image=.\pdu-firmware.bin
2026-07-04 10:30:07 INFO 10.42.98.137: uploaded image info - version=4.3.13; valid=True; compatible=True; ...
2026-07-04 10:30:07 INFO 10.42.98.137: starting firmware update from 4.3.0.5-51180 to 4.3.13
2026-07-04 10:30:17 INFO 10.42.98.137: update status - state=UPDATE
2026-07-04 10:34:52 INFO 10.42.98.137: update status - state=SUCCESS
2026-07-04 10:35:10 INFO 10.42.98.137: OK - changed; old=4.3.0.5-51180; image_version=4.3.13; new=4.3.13
2026-07-04 10:35:10 INFO Done. OK=1 Failed=0
```
The line to look for: `<IP>: OK - changed`.

**Step 4 — rerun safely, any time.** If a PDU already has the image version installed, it reports `OK - skipped_same_version` — this is expected, not a problem. Use `--allow-same-version` if you specifically want to reinstall the same version anyway.

**Recommended rollout process:**
1. Put one test PDU in `pdus.txt`.
2. `--check` its current firmware.
3. `--update --dry-run` it.
4. If that looks good, run the live update.
5. Confirm the new version with `--check`.
6. Add the remaining PDU IPs to `pdus.txt`.
7. Check → dry-run → live-update again for the full batch.
8. Save the log file for your records.

**Other common commands:**
```powershell
# Different IP list file
python .\bootstrap_pdu_firmware.py --ips .\my-pdus.txt --check

# Longer timeout for slow networks
python .\bootstrap_pdu_firmware.py --ips .\pdus.txt --check --timeout 30
```
Keep `--concurrency 1` (the default) for first use, and generally for firmware updates — an interrupted or failed update on one PDU is a bigger operational concern than an interrupted password change.

## Command-line options

| Flag | Default | Description |
|---|---|---|
| `--check` | — | Read-only firmware version check. Required unless `--update` is used; can't combine with it |
| `--update` | — | Upload and install a firmware image. Required unless `--check` is used; can't combine with it; requires `--image` |
| `--ips PATH` | `pdus.txt` | IP list file |
| `--image PATH` | none | Firmware image file to upload. Required with `--update`; unused with `--check` |
| `--password PASSWORD` | *(env/prompt)* | Admin password. Supported but not recommended on the CLI — can appear in shell history |
| `--timeout SECONDS` | `10` | Timeout for ordinary API calls (login, reading version, etc.) |
| `--update-timeout SECONDS` | `1800` (30 min) | Max time to wait for the update itself to report success/failure |
| `--availability-timeout SECONDS` | `600` (10 min) | Max time to wait for the PDU to respond again after it reboots post-update |
| `--poll-interval SECONDS` | `10` | How often to check update status while waiting |
| `--concurrency NUMBER` | `1` | Number of PDUs processed in parallel. Invalid values like `0` or negative numbers are blocked |
| `--dry-run` | off | For `--update`: check connectivity/current version only, no upload or install. No effect with `--check` |
| `--allow-same-version` | off | Install even if the image version matches what's currently installed |
| `-v`, `--verbose` | off | Debug logging |
| `--log-file PATH` | none | Also write logs to this file |
| `--no-insecure` | — | Require valid, trusted HTTPS certificates |
| `--insecure` | on by default | Disable HTTPS certificate verification (default, since most PDUs use self-signed certs) |

## Common results

| Result | Meaning |
|---|---|
| `OK - firmware=<version>` | `--check` mode: currently installed version |
| `OK - would_update` | `--update --dry-run`: reports current version and the image that would be uploaded; nothing changed |
| `OK - changed` | Update completed, confirmed successful, new version confirmed afterward |
| `OK - skipped_same_version` | Image version matched what's installed; discarded, nothing installed |
| `ERROR - uploaded firmware image is not valid` | PDU rejected the file as an invalid firmware image |
| `ERROR - uploaded firmware image is not compatible with this device` | PDU accepted the file as valid, but it's not compatible with this specific model |
| `ERROR - uploaded firmware image appears older than installed firmware` | Looked like a downgrade — discarded; this script never automates downgrades |
| `ERROR - firmware update did not report success` | Update started but didn't succeed before `--update-timeout`, or reported failure |
| `ERROR - device did not return before timeout` | Update likely finished, but the PDU didn't respond again within `--availability-timeout` |

## Troubleshooting

| Problem | Fix |
|---|---|
| `No IPs found in pdus.txt` | Make sure `pdus.txt` exists in the folder you're running from, with one IP per line |
| `python is not recognized` / `py is not recognized` | Python isn't installed correctly or isn't on PATH — reinstall and enable the PATH option |
| `No module named raritan` | Activate your virtual environment and run `python -m pip install raritan` |
| `--image is required with --update.` | Add `--image` pointing at your firmware `.bin` file |
| `Firmware image not found` / `Firmware image is empty` | Check the path; confirm the file downloaded completely and isn't 0 bytes |
| `uploaded firmware image is not valid` | The file likely isn't a genuine/uncorrupted Raritan firmware image — re-download it |
| `uploaded firmware image is not compatible with this device` | Wrong firmware for this PDU model — confirm you have the correct file for this specific model/family |
| `uploaded firmware image appears older than installed firmware` | This script won't automate downgrades — do it manually per Raritan's guidance, or contact Raritan Support |
| `firmware update did not report success` | Check the PDU directly — it may still be mid-update or may have failed. Consider raising `--update-timeout` for slower devices |
| `device did not return before timeout` | Update likely finished but took longer than `--availability-timeout` — check the PDU directly and consider raising the timeout |
| Connection timed out / refused / no route to host | Check the IP, that the PDU is online, that your network can reach it, and firewall rules for TCP 443 |
| Some PDUs updated, others failed | Review the failed IPs, check each directly, fix the issue, `--check` them before attempting `--update` again |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All PDUs processed successfully |
| `1` | Local setup/input problem (no IPs, missing/empty image, missing `--image` with `--update`, invalid concurrency or poll-interval) before any PDU was contacted |
| `2` | One or more PDUs failed |
| `130` | Interrupted with Ctrl+C |

## A note on Ctrl+C during --update

If you interrupt the script after a PDU's firmware update has already started, **the update keeps running on that PDU** — interrupting the script only stops the script from *monitoring* it, not the update itself. Check any PDU that had already started an update directly before assuming anything about its state.

## Safety notes

- Run `--check` first to see what every PDU is currently running.
- Run `--update --dry-run` before any live update.
- Keep `--concurrency 1`, especially for firmware — an interrupted update is a bigger deal than an interrupted password change.
- Keep the IP list small and controlled for a first live update.
- Confirm you have the correct, current firmware image for this PDU model before running against your full fleet.
- Don't interrupt (Ctrl+C) a live update once it's started for a given PDU — see above.
- Never paste real passwords into tickets, chat, email, or screenshots.
- Prefer the interactive password prompt over `--password` on the CLI.
- Close PowerShell after using `$env:PDU_ADMIN_PASSWORD`.
- Save a log file for audit records.

## Source references

- [Python for Windows documentation](https://docs.python.org/3/using/windows.html)
- [Python downloads](https://www.python.org/downloads/)
- [Python install manager release page](https://www.python.org/downloads/release/pymanager-262/)
- [Raritan Python package on PyPI](https://pypi.org/project/raritan/)
