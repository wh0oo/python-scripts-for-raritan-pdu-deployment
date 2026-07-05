#!/usr/bin/env python3
"""
Set Raritan PDU names from reverse DNS.

Reads a list of PDU IP addresses, performs a reverse DNS lookup for each IP,
derives the PDU name from the hostname without the domain, uppercases it, and
sets the PDU's user-defined name to that value.

Generic example:
    10.10.5.130 -> pdu-row3-a12.example.com -> PDU-ROW3-A12

Safety rule:
    Before logging in to any PDU, the script resolves and validates reverse DNS
    for every IP. An IP with missing/unusable reverse DNS, or one that collides
    with another IP's derived name, is skipped and reported as failed; the
    script still contacts and processes every other IP normally.

Input file:
    pdus.txt

Example pdus.txt:
    10.10.5.130
    10.10.5.131  # optional inline comment

Common examples:
    python .\\bootstrap_pdu_names_from_dns.py --ips .\\pdus.txt --dry-run -v
    python .\\bootstrap_pdu_names_from_dns.py --ips .\\pdus.txt -v --log-file .\\pdu-names.log

Implementation note:
    This script intentionally uses raw JSON-RPC calls for /model/pdu/0's
    getSettings and setSettings, instead of the typed pdumodel.Pdu.getSettings()
    wrapper. On tested hardware, the typed Raritan Python binding failed while
    decoding the full Settings structure, because the device's response
    omitted fields such as inletWiring. This script only needs the PDU name,
    so it reads the raw getSettings response directly and writes back only
    the sparse name field with setSettings, sidestepping that decode bug
    entirely. See get_pdu_name() and set_pdu_name_only() for details.

    Possible failure modes to be aware of:
    - The getSettings response doesn't contain '_ret_' or 'name' -- surfaces
      as a clear RuntimeError rather than a silent None.
    - setSettings returns 1, meaning the PDU rejected the supplied settings
      (for example, if this device/API doesn't accept a sparse settings
      update containing only the name field).
    - The account can authenticate but lacks permission to modify PDU
      settings -- login succeeding does not guarantee setSettings will.
    - The /model/pdu/0 resource path is specific to the tested PX-series
      Xerus API; a different product/API family could use a different path.
    - Normal per-IP network, timeout, TLS, and password failures still apply
      exactly as they did before this change.
"""

import argparse
import getpass
import ipaddress
import logging
import os
import re
import socket
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import raritan.rpc
from raritan.rpc import usermgmt


_PDU_TARGET = "/model/pdu/0"

DEFAULT_PASSWORD_ENV = "PDU_ADMIN_PASSWORD"

log = logging.getLogger("pdu_names_from_dns")

_HOST_LABEL_RE = re.compile(r"^[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?$")


def read_ips(path):
    """Read one IP per line from path.

    Blank lines are ignored.
    Full-line comments are ignored.
    Inline comments are allowed after '#'.
    Duplicate IPs are skipped.
    Invalid IPs fail loudly with the line number.
    """
    ips = []
    seen = set()

    with open(path, "r", encoding="utf-8") as f:
        for line_number, raw_line in enumerate(f, start=1):
            line = raw_line.split("#", 1)[0].strip()

            if not line:
                continue

            try:
                ipaddress.ip_address(line)
            except ValueError:
                raise ValueError(f"{path}:{line_number}: invalid IP address: {line}")

            if line in seen:
                log.debug("Skipping duplicate IP: %s", line)
                continue

            seen.add(line)
            ips.append(line)

    return ips


def make_agent(ip, username, password, timeout, verify_cert):
    return raritan.rpc.Agent(
        "https",
        ip,
        username,
        password,
        disable_certificate_verification=not verify_cert,
        timeout=timeout,
    )


def test_login(ip, username, password, timeout, verify_cert):
    """Use a small authenticated call to verify login.

    Do not use pdumodel.Pdu(...).getMetaData() here. On at least one tested
    PDU/API/library combination, that call failed with KeyError:
    'supportedInletWirings'. The user API is enough to prove auth works.
    """
    agent = make_agent(ip, username, password, timeout, verify_cert)
    usermgmt.User(f"/auth/user/{username}", agent).getInfo()
    return agent


def reverse_dns_name(ip):
    """Return PTR hostname for IP, or raise a loud error."""
    try:
        fqdn = socket.gethostbyaddr(ip)[0]
    except socket.herror as e:
        raise RuntimeError(f"NO REVERSE DNS / PTR RECORD for {ip}; skipping with no changes") from e
    except socket.gaierror as e:
        raise RuntimeError(f"DNS lookup failed for {ip}; skipping with no changes") from e

    fqdn = fqdn.rstrip(".").strip()

    if not fqdn:
        raise RuntimeError(f"reverse DNS for {ip} returned an empty hostname; skipping with no changes")

    return fqdn


def target_name_from_fqdn(fqdn):
    """Convert FQDN to the desired PDU name.

    Example:
        pdu-row3-a12.example.com -> PDU-ROW3-A12
    """
    short_name = fqdn.split(".", 1)[0].upper().strip()

    if not short_name:
        raise RuntimeError(f"hostname '{fqdn}' produced an empty PDU name")

    if not _HOST_LABEL_RE.match(short_name):
        raise RuntimeError(
            f"hostname '{fqdn}' produced invalid PDU name '{short_name}'"
        )

    return short_name


def preflight_targets(ips):
    """Resolve and validate reverse DNS and target names for every IP,
    without aborting the whole batch over one or a few problems.

    Returns (targets, failures):
        targets[ip]  = {"fqdn": fqdn, "target_name": target_name}
            Safe to process normally.
        failures[ip] = "reason string"
            This IP will not be contacted; everything else proceeds.

    Two things can put an IP in failures:
    - Missing/unusable reverse DNS, or a hostname that doesn't produce a
      valid PDU name. This only affects that one IP.
    - A target name collision: two or more IPs derive the same PDU name.
      Since there's no way to tell which one (if either) should actually
      get that name, ALL of the colliding IPs are excluded, not just the
      second one seen.
    """
    targets = {}
    failures = {}
    name_to_ips = {}

    for ip in ips:
        try:
            fqdn = reverse_dns_name(ip)
            target_name = target_name_from_fqdn(fqdn)
        except Exception as e:
            failures[ip] = str(e)
            continue

        targets[ip] = {"fqdn": fqdn, "target_name": target_name}
        name_to_ips.setdefault(target_name, []).append(ip)

        log.info(
            "%s: reverse DNS=%s; target_name=%s",
            ip,
            fqdn,
            target_name,
        )

    for target_name, colliding_ips in name_to_ips.items():
        if len(colliding_ips) <= 1:
            continue
        for ip in colliding_ips:
            others = ", ".join(other for other in colliding_ips if other != ip)
            failures[ip] = (
                f"duplicate target PDU name '{target_name}' also derived for "
                f"{others}; skipping all IPs that collided on this name"
            )
            del targets[ip]

    return targets, failures


def get_pdu_name(agent):
    """Read the PDU's user-defined name via a raw JSON-RPC call, bypassing
    pdumodel.Pdu.Settings.decode().

    On at least one tested PDU/firmware/library combination, getSettings()
    through the typed pdumodel.Pdu API raises KeyError: 'inletWiring'. This
    is a decode-time bug in the raritan package itself: Pdu.Settings.decode()
    has a line shaped like

        inletWiring = ... if 'inletWiring' in json or not useDefaults else ...

    getSettings() always decodes with useDefaults=False, which makes
    "not useDefaults" True, which makes the whole condition True regardless
    of whether the key is actually present -- so it unconditionally tries
    json['inletWiring']. If a PDU's real response omits that field (as at
    least one tested device does), decode() raises a bare KeyError instead
    of returning a usable Settings object.

    Reading only the 'name' key directly from the raw response avoids the
    broken field entirely, since we never ask the library to decode the
    rest of the struct.

    Raises RuntimeError if the raw response doesn't have the expected shape
    (missing '_ret_', '_ret_' isn't a dict, or 'name' is absent from it) --
    rather than silently returning None and letting the caller mistake that
    for "this PDU currently has no name".
    """
    rsp = agent.json_rpc(_PDU_TARGET, "getSettings", {})

    settings = rsp.get("_ret_")
    if not isinstance(settings, dict):
        raise RuntimeError(f"unexpected getSettings response shape: {rsp!r}")

    if "name" not in settings:
        raise RuntimeError(f"getSettings response did not include a 'name' field: {rsp!r}")

    return settings["name"]


def set_pdu_name_only(agent, target_name):
    """Set the PDU's user-defined name via a raw JSON-RPC call, bypassing
    the typed pdumodel.Pdu.Settings object entirely.

    setSettings()'s own API documentation states the Settings structure may
    be sent "sparse": fields missing from the request are left unchanged on
    the device. Sending only {"name": ...} is therefore a supported partial
    update, and it sidesteps needing a full, successfully-decoded Settings
    object -- which get_pdu_name() above cannot always provide, see its
    docstring -- as well as needing to supply a valid inletWiring value
    ourselves.

    Returns the same integer code setSettings() would: 0 = OK, 1 = invalid
    parameters.

    Raises RuntimeError if the raw response is missing '_ret_' entirely --
    an unexpected shape that shouldn't be silently treated as any particular
    return code.
    """
    rsp = agent.json_rpc(_PDU_TARGET, "setSettings", {"settings": {"name": target_name}})

    if "_ret_" not in rsp:
        raise RuntimeError(f"unexpected setSettings response shape: {rsp!r}")

    return rsp["_ret_"]


def set_pdu_name(ip, fqdn, target_name, username, password, timeout, verify_cert, dry_run):
    agent = test_login(ip, username, password, timeout, verify_cert)

    current_name = get_pdu_name(agent)

    if current_name == target_name:
        return {
            "ip": ip,
            "result": "already_named",
            "fqdn": fqdn,
            "current": current_name,
            "target": target_name,
        }

    if dry_run:
        return {
            "ip": ip,
            "result": "would_change",
            "fqdn": fqdn,
            "current": current_name,
            "target": target_name,
        }

    ret = set_pdu_name_only(agent, target_name)

    if ret != 0:
        return {
            "ip": ip,
            "result": f"setSettings_failed_ret_{ret}",
            "fqdn": fqdn,
            "current": current_name,
            "target": target_name,
        }

    verified_name = get_pdu_name(agent)

    if verified_name != target_name:
        return {
            "ip": ip,
            "result": "verify_failed",
            "fqdn": fqdn,
            "current": current_name,
            "target": target_name,
            "verified": verified_name,
        }

    return {
        "ip": ip,
        "result": "changed",
        "fqdn": fqdn,
        "old": current_name,
        "new": verified_name,
        "target": target_name,
    }


def process_one(ip, target_info, username, password, args):
    try:
        result = set_pdu_name(
            ip=ip,
            fqdn=target_info["fqdn"],
            target_name=target_info["target_name"],
            username=username,
            password=password,
            timeout=args.timeout,
            verify_cert=not args.insecure,
            dry_run=args.dry_run,
        )
        return result, None
    except Exception as e:
        return {"ip": ip}, e


def parse_args():
    parser = argparse.ArgumentParser(
        description="Set Raritan PDU names from reverse DNS."
    )

    parser.add_argument(
        "--ips",
        default="pdus.txt",
        help="File with one PDU IP address per line. Default: pdus.txt",
    )
    parser.add_argument(
        "--username",
        default="admin",
        help="PDU username. Default: admin",
    )
    parser.add_argument(
        "--password",
        default=None,
        help=(
            "PDU password. If omitted, read from the "
            f"{DEFAULT_PASSWORD_ENV} environment variable, or prompt interactively. "
            "Avoid passing this on the command line because it may be visible in "
            "shell history and process listings."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Connection timeout in seconds. Default: 10",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of PDUs to process in parallel. Default: 1",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=True,
        help=(
            "Disable TLS certificate verification (default: on, since most PDUs "
            "use self-signed certs). Pass --no-insecure to require valid certificates."
        ),
    )
    parser.add_argument(
        "--no-insecure",
        action="store_false",
        dest="insecure",
        help="Require valid TLS certificates (disables --insecure).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change but do not set PDU names.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path to also write logs to a file.",
    )

    return parser.parse_args()


def setup_logging(verbose, log_file):
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [logging.StreamHandler(sys.stdout)]

    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def log_success(result):
    ip = result.get("ip")
    name = result.get("result")

    if name == "already_named":
        log.info(
            '%s: OK - already_named; reverse DNS=%s; current="%s"',
            ip,
            result.get("fqdn"),
            result.get("current"),
        )
    elif name == "would_change":
        log.info(
            '%s: OK - would_change; reverse DNS=%s; current="%s"; target="%s"',
            ip,
            result.get("fqdn"),
            result.get("current"),
            result.get("target"),
        )
    elif name == "changed":
        log.info(
            '%s: OK - changed; reverse DNS=%s; old="%s"; new="%s"',
            ip,
            result.get("fqdn"),
            result.get("old"),
            result.get("new"),
        )
    else:
        log.info("%s: OK - %s", ip, name)


def log_failure(result):
    """Log a non-exception failure result with full context.

    set_pdu_name() always returns fqdn/current/target (and, for verify_failed,
    the mismatched verified name) alongside the bare result string. Surface
    all of it here instead of just the result string, since "ERROR -
    verify_failed" alone gives no way to tell what name was intended, what
    DNS resolved to, or what the PDU actually ended up reporting.
    """
    ip = result.get("ip")
    name = result.get("result")
    fqdn = result.get("fqdn")

    if name == "verify_failed":
        log.error(
            '%s: ERROR - verify_failed; reverse DNS=%s; current="%s"; target="%s"; verified_as="%s"',
            ip,
            fqdn,
            result.get("current"),
            result.get("target"),
            result.get("verified"),
        )
    elif fqdn is not None:
        # e.g. setSettings_failed_ret_1, or any other future dict-based
        # failure that carries fqdn/current/target context.
        log.error(
            '%s: ERROR - %s; reverse DNS=%s; current="%s"; target="%s"',
            ip,
            name,
            fqdn,
            result.get("current"),
            result.get("target"),
        )
    else:
        log.error("%s: ERROR - %s", ip, name)


def main():
    args = parse_args()
    setup_logging(args.verbose, args.log_file)

    if args.concurrency < 1:
        log.error("Concurrency must be at least 1.")
        sys.exit(1)

    try:
        ips = read_ips(args.ips)
    except Exception as e:
        log.error("%s", e)
        sys.exit(1)

    if not ips:
        log.error("No IPs found in %s", args.ips)
        sys.exit(1)

    mode = "DRY RUN" if args.dry_run else "LIVE"
    log.info(
        "Preflighting reverse DNS for %d PDU(s) [%s]...",
        len(ips),
        mode,
    )

    try:
        targets, preflight_failures = preflight_targets(ips)
    except KeyboardInterrupt:
        log.warning("Interrupted during DNS preflight -- exiting.")
        sys.exit(130)

    ok = 0
    failed = 0
    failed_ips = []
    interrupted = False

    for ip, reason in preflight_failures.items():
        failed += 1
        failed_ips.append(ip)
        log.error("%s: ERROR - %s", ip, reason)

    if not targets:
        log.info("No PDUs passed DNS preflight; nothing to process.")
        log.info("Done. OK=%d Failed=%d", ok, failed)
        if failed_ips:
            log.info("Failed IPs: %s", ", ".join(failed_ips))
        sys.exit(2)

    password = args.password or os.environ.get(DEFAULT_PASSWORD_ENV)
    if not password:
        password = getpass.getpass(
            f"PDU password for {args.username} (or set ${DEFAULT_PASSWORD_ENV}): "
        )

    log.info(
        "Processing %d PDU(s) [%s, concurrency=%d, verify_cert=%s]...",
        len(targets),
        mode,
        args.concurrency,
        not args.insecure,
    )

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(
                process_one,
                ip,
                targets[ip],
                args.username,
                password,
                args,
            ): ip
            for ip in targets
        }

        try:
            for future in as_completed(futures):
                ip = futures[future]
                result, exc = future.result()

                if exc is not None:
                    failed += 1
                    failed_ips.append(ip)
                    log.error("%s: ERROR - %s", ip, exc)
                elif result.get("result") in ("already_named", "would_change", "changed"):
                    ok += 1
                    log_success(result)
                else:
                    failed += 1
                    failed_ips.append(ip)
                    log_failure(result)

        except KeyboardInterrupt:
            interrupted = True
            log.warning("Interrupted -- cancelling remaining PDUs and shutting down...")
            for f in futures:
                f.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

    log.info(
        "Done. OK=%d Failed=%d%s",
        ok,
        failed,
        " (interrupted)" if interrupted else "",
    )

    if failed_ips:
        log.info("Failed IPs: %s", ", ".join(failed_ips))

    if interrupted:
        sys.exit(130)

    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()