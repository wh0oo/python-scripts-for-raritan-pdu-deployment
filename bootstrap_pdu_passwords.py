#!/usr/bin/env python3
"""
Bootstrap Raritan PDU admin passwords from a list of IPs.

Reads a list of PDU IP addresses, logs in as 'admin' using either the
current password or a known default/bootstrap password, and sets a new
admin password on each device.
"""
import argparse
import getpass
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import raritan.rpc
from raritan.rpc import usermgmt

DEFAULT_OLD_PASSWORD_ENV = "PDU_OLD_PASSWORD"

log = logging.getLogger("pdu_bootstrap")


def read_ips(path):
    ips = []
    seen = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line in seen:
                log.debug("Skipping duplicate IP: %s", line)
                continue
            seen.add(line)
            ips.append(line)
    return ips


def make_agent(ip, password, timeout, verify_cert):
    return raritan.rpc.Agent(
        "https",
        ip,
        "admin",
        password,
        disable_certificate_verification=not verify_cert,
        timeout=timeout,
    )


def test_login(ip, password, timeout, verify_cert):
    agent = make_agent(ip, password, timeout, verify_cert)
    usermgmt.User("/auth/user/admin", agent).getInfo()
    return agent


_HTTP_STATUS_RE = re.compile(r"HTTP Error (\d+)")

# Note: raritan.rpc.HttpException carries NO structured status code -- in the
# installed library it's defined as a bare `class HttpException(Exception):
# pass`. Worse, the *same* exception class is raised both for real HTTP error
# responses ("HTTP Error %d\n...") and for transport-level failures like
# timeouts/DNS/connection-refused ("HTTP request failed", wrapping the
# underlying error). The only reliable way to tell them apart is to parse
# the message, so we do that explicitly rather than pretending there's a
# clean structured way to distinguish them.


def get_http_status(exc):
    """Extract the numeric HTTP status code from an HttpException's message,
    or None if this exception doesn't carry one (e.g. a transport-level
    failure such as a timeout or connection error)."""
    m = _HTTP_STATUS_RE.search(str(exc))
    return int(m.group(1)) if m else None


def is_http_451(exc):
    return get_http_status(exc) == 451


def is_auth_failure(exc):
    """True if this HttpException represents a rejected/incorrect credential
    (HTTP 401/403), as opposed to a transport-level failure that happens to
    share the same exception class."""
    return get_http_status(exc) in (401, 403)


def password_result_name(ret):
    """Translate a setAccountPassword() return code into a readable name,
    using the constants the library itself defines. Falls back to a numbered
    'unknown_password_error_N' if a constant isn't present in this version
    of the library (getattr(..., None) protects against that -- though note
    if two different constants were both missing on some future/older
    library version, they'd collide on the same None key. Not a concern with
    the currently installed 4.4.0.52884 package, where all of these exist)."""
    known = {
        getattr(usermgmt.User, "ERR_PASSWORD_UNCHANGED", None): "password_unchanged",
        getattr(usermgmt.User, "ERR_PASSWORD_EMPTY", None): "password_empty",
        getattr(usermgmt.User, "ERR_PASSWORD_TOO_SHORT", None): "password_too_short",
        getattr(usermgmt.User, "ERR_PASSWORD_TOO_LONG", None): "password_too_long",
        getattr(usermgmt.User, "ERR_PASSWORD_CTRL_CHARS", None): "password_has_control_chars",
        getattr(usermgmt.User, "ERR_PASSWORD_NEED_LOWER", None): "password_needs_lowercase",
        getattr(usermgmt.User, "ERR_PASSWORD_NEED_UPPER", None): "password_needs_uppercase",
        getattr(usermgmt.User, "ERR_PASSWORD_NEED_NUMERIC", None): "password_needs_number",
        getattr(usermgmt.User, "ERR_PASSWORD_NEED_SPECIAL", None): "password_needs_special",
        getattr(usermgmt.User, "ERR_PASSWORD_IN_HISTORY", None): "password_in_history",
        getattr(usermgmt.User, "ERR_PASSWORD_TOO_SHORT_FOR_SNMP", None): "password_too_short_for_snmp",
    }
    return known.get(ret, f"unknown_password_error_{ret}")


def change_admin_password(ip, old_password, new_password, timeout, verify_cert, dry_run=False):
    if dry_run:
        # Dry-run means exactly: can we reach this PDU with the old/bootstrap
        # password, and would it be eligible for a change? Never touch the
        # new-password login path here -- the new password may just be a
        # placeholder in dry-run mode, and there's no live change to make.
        agent = make_agent(ip, old_password, timeout, verify_cert)
        try:
            usermgmt.User("/auth/user/admin", agent).getInfo()
        except raritan.rpc.HttpException as e:
            if not is_http_451(e):
                raise
        return "would_change"

    # First, see if this PDU is already using the new password.
    try:
        test_login(ip, new_password, timeout, verify_cert)
        return "already_changed"
    except raritan.rpc.HttpException as e:
        if not is_auth_failure(e):
            # Not a rejected-credential response (401/403) -- this is a
            # transport-level failure (timeout, DNS, connection refused,
            # etc.) wrapped in the same exception class. Don't swallow it;
            # let the caller report a real connectivity failure instead of
            # silently treating it as "not yet changed".
            raise
        # Otherwise: wrong/unknown password with the new credentials --
        # expected on a PDU that hasn't been changed yet, keep trying below.

    # Now try the factory/default/current bootstrap password.
    agent = make_agent(ip, old_password, timeout, verify_cert)
    try:
        # This may succeed normally, or it may throw HTTP 451 when the PDU
        # requires the default admin password to be changed.
        usermgmt.User("/auth/user/admin", agent).getInfo()
    except raritan.rpc.HttpException as e:
        if not is_http_451(e):
            raise

    admin = usermgmt.User("/auth/user/admin", agent)
    ret = admin.setAccountPassword(new_password)
    if ret == 0:
        # Verify the new password actually works.
        test_login(ip, new_password, timeout, verify_cert)
        return "changed"
    return password_result_name(ret)


def process_one(ip, old_password, new_password, timeout, verify_cert, dry_run):
    try:
        result = change_admin_password(
            ip=ip,
            old_password=old_password,
            new_password=new_password,
            timeout=timeout,
            verify_cert=verify_cert,
            dry_run=dry_run,
        )
        return ip, result, None
    except Exception as e:
        return ip, None, e


def parse_args():
    parser = argparse.ArgumentParser(
        description="Bootstrap Raritan PDU admin passwords from a list of IPs."
    )
    parser.add_argument(
        "--ips",
        default="pdus.txt",
        help="File with one PDU IP address per line. Default: pdus.txt",
    )
    parser.add_argument(
        "--old-password",
        default=None,
        help=(
            "Current/default admin password. If omitted, read from the "
            f"{DEFAULT_OLD_PASSWORD_ENV} environment variable, or prompted "
            "interactively. Avoid passing this on the command line -- it is "
            "visible in shell history and process listings."
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
            "Disable TLS certificate verification (default: on, since most "
            "PDUs use self-signed certs). Pass --no-insecure to require "
            "valid certificates."
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
        help="Check connectivity/credentials only; do not change any passwords.",
    )
    parser.add_argument(
        "--min-length",
        type=int,
        default=8,
        help="Minimum acceptable length for the new password. Default: 8",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path to also write logs to a file (for audit records).",
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


def main():
    args = parse_args()
    setup_logging(args.verbose, args.log_file)

    if args.concurrency < 1:
        log.error("Concurrency must be at least 1.")
        sys.exit(1)

    ips = read_ips(args.ips)
    if not ips:
        log.error("No IPs found in %s", args.ips)
        sys.exit(1)

    old_password = args.old_password or os.environ.get(DEFAULT_OLD_PASSWORD_ENV)
    if not old_password:
        old_password = getpass.getpass(
            f"Current/default admin password (or set ${DEFAULT_OLD_PASSWORD_ENV}): "
        )

    if args.dry_run:
        new_password = "unused-in-dry-run"
    else:
        new_password = getpass.getpass("New admin password: ")
        confirm = getpass.getpass("Confirm new admin password: ")
        if new_password != confirm:
            log.error("Passwords do not match.")
            sys.exit(1)
        if len(new_password) < args.min_length:
            log.error(
                "New password must be at least %d characters.", args.min_length
            )
            sys.exit(1)
        if new_password == old_password:
            log.error(
                "New password must not be the same as the current/default password."
            )
            sys.exit(1)

    mode = "DRY RUN" if args.dry_run else "LIVE"
    log.info(
        "Processing %d PDU(s) [%s, concurrency=%d, verify_cert=%s]...",
        len(ips), mode, args.concurrency, not args.insecure,
    )

    ok = 0
    failed = 0
    failed_ips = []
    interrupted = False

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(
                process_one,
                ip,
                old_password,
                new_password,
                args.timeout,
                not args.insecure,
                args.dry_run,
            ): ip
            for ip in ips
        }
        try:
            for future in as_completed(futures):
                ip, result, exc = future.result()
                if exc is not None:
                    failed += 1
                    failed_ips.append(ip)
                    log.error("%s: ERROR - %s", ip, exc)
                elif result in ("changed", "already_changed", "password_unchanged", "would_change"):
                    ok += 1
                    log.info("%s: OK - %s", ip, result)
                else:
                    failed += 1
                    failed_ips.append(ip)
                    log.error("%s: ERROR - %s", ip, result)
        except KeyboardInterrupt:
            interrupted = True
            log.warning("Interrupted -- cancelling remaining PDUs and shutting down...")
            for f in futures:
                f.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

    log.info("Done. OK=%d Failed=%d%s", ok, failed, " (interrupted)" if interrupted else "")
    if failed_ips:
        log.info("Failed IPs: %s", ", ".join(failed_ips))
    if interrupted:
        sys.exit(130)
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
