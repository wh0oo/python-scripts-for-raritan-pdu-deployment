#!/usr/bin/env python3
"""
Check or update Raritan PDU firmware for a list of IPs.

Reads one PDU IP address per line from pdus.txt by default.

Common examples:

    python .\\bootstrap_pdu_firmware.py --ips .\\pdus.txt --check -v
    python .\\bootstrap_pdu_firmware.py --ips .\\pdus.txt --update --image .\\pdu-firmware.bin -v --log-file .\\pdu-firmware.log

The update path is intentionally conservative:
- dry-run never uploads or starts an update
- concurrency defaults to 1
- if the uploaded image is not valid or not compatible, the update is not started
- same-version updates are skipped unless --allow-same-version is used
- downgrade-looking updates are discarded and treated as failures
"""

import argparse
import getpass
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import raritan.rpc
from raritan.rpc import firmware, usermgmt

DEFAULT_PASSWORD_ENV = "PDU_ADMIN_PASSWORD"

log = logging.getLogger("pdu_firmware")

_HTTP_STATUS_RE = re.compile(r"HTTP Error (\d+)")
_VERSION_NUM_RE = re.compile(r"\d+")


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
    """Use a small authenticated call to verify login.

    Do not use pdumodel.Pdu(...).getMetaData() here. On at least one tested
    PDU/API/library combination, that call failed with KeyError:
    'supportedInletWirings'. The admin user API is enough to prove auth works.
    """
    agent = make_agent(ip, password, timeout, verify_cert)
    usermgmt.User("/auth/user/admin", agent).getInfo()
    return agent


def get_http_status(exc):
    m = _HTTP_STATUS_RE.search(str(exc))
    return int(m.group(1)) if m else None


def version_numbers(version):
    """Return a tuple of numbers found in a version string.

    This is deliberately simple. It handles versions such as:
      4.3.13
      4.3.13-52884
      Xerus 4.3.13 build 52884
    """
    if version is None:
        return ()
    return tuple(int(x) for x in _VERSION_NUM_RE.findall(str(version)))


def compare_versions(left, right):
    """Compare two version strings.

    Returns:
      -1 if left appears older than right
       0 if they appear equal
       1 if left appears newer than right

    If either side cannot be parsed, return None.
    """
    a = version_numbers(left)
    b = version_numbers(right)
    if not a or not b:
        return None

    max_len = max(len(a), len(b))
    a = a + (0,) * (max_len - len(a))
    b = b + (0,) * (max_len - len(b))

    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def attr(obj, name, default="unknown"):
    return getattr(obj, name, default)


def image_info_summary(image_info):
    if image_info is None:
        return "image_info=none"

    parts = []
    for name in (
        "version",
        "valid",
        "compatible",
        "product",
        "platform",
        "min_required_version",
        "min_downgrade_version",
    ):
        if hasattr(image_info, name):
            parts.append(f"{name}={getattr(image_info, name)}")

    if parts:
        return "; ".join(parts)
    return str(image_info)


def update_status_summary(status):
    if status is None:
        return "state=WAITING"

    parts = []
    for name in ("state", "elapsed", "estimated", "error_message"):
        if hasattr(status, name):
            parts.append(f"{name}={getattr(status, name)}")

    if parts:
        return "; ".join(parts)
    return str(status)


def get_status_state(status):
    state = getattr(status, "state", None)
    if state is None:
        return None
    return str(state)


def newest_history_entry(history):
    if not history:
        return None
    return history[-1]


def history_entry_matches_image(entry, image_version):
    if entry is None or not image_version:
        return False
    entry_version = getattr(entry, "imageVersion", None)
    if not entry_version:
        return False
    return compare_versions(entry_version, image_version) == 0 or str(entry_version) == str(image_version)


def history_entry_successful(entry):
    if entry is None:
        return False

    status = getattr(entry, "status", None)
    if status is None:
        return False

    if status == getattr(firmware.UpdateHistoryStatus, "SUCCESSFUL", object()):
        return True

    return "SUCCESS" in str(status).upper()


def poll_update(ip, agent, fw_proxy, image_version, poll_interval, update_timeout):
    """Poll firmware update status until SUCCESS, FAIL, or timeout.

    Raritan documents that normal RPC requests stop shortly after startUpdate().
    The status endpoint is /cgi-bin/fwupdate_progress.cgi. The Python bindings
    expose that endpoint as firmware.FirmwareUpdateStatus.
    """
    deadline = time.monotonic() + update_timeout
    status_proxy = firmware.FirmwareUpdateStatus("/cgi-bin/fwupdate_progress.cgi", agent)
    last_state = None

    while time.monotonic() < deadline:
        try:
            status = status_proxy.getStatus()
            state = get_status_state(status)

            if state != last_state:
                log.info("%s: update status - %s", ip, update_status_summary(status))
                last_state = state
            else:
                log.debug("%s: update status - %s", ip, update_status_summary(status))

            if state == "SUCCESS":
                return "success"
            if state == "FAIL":
                return "failed"

            # Some devices return NONE after reboot. Raritan's own bulk update
            # example checks update history at that point.
            if state == "NONE":
                try:
                    history = fw_proxy.getUpdateHistory()
                    entry = newest_history_entry(history)
                    if history_entry_matches_image(entry, image_version):
                        if history_entry_successful(entry):
                            return "success"
                        return "failed"
                except Exception as e:
                    log.debug("%s: update history check failed while polling: %s", ip, e)

        except Exception as e:
            # During the update/reboot, temporary communication failures are expected.
            log.debug("%s: waiting for firmware update status: %s", ip, e)

        time.sleep(poll_interval)

    return "timeout"


def wait_for_version(ip, password, verify_cert, timeout, wait_timeout, expected_version=None):
    """Wait for the PDU management interface to answer getVersion() again."""
    deadline = time.monotonic() + wait_timeout
    last_error = None

    while time.monotonic() < deadline:
        try:
            agent = test_login(ip, password, timeout, verify_cert)
            fw_proxy = firmware.Firmware("/firmware", agent)
            version = fw_proxy.getVersion()
            if expected_version:
                cmp_result = compare_versions(version, expected_version)
                if cmp_result == 0 or str(version) == str(expected_version):
                    return version
            else:
                return version
        except Exception as e:
            last_error = e
            log.debug("%s: waiting for device to come back: %s", ip, e)
            time.sleep(10)

    if last_error:
        raise RuntimeError(f"device did not return before timeout; last error: {last_error}")
    raise RuntimeError("device did not return before timeout")


def check_one(ip, password, timeout, verify_cert):
    agent = test_login(ip, password, timeout, verify_cert)
    fw_proxy = firmware.Firmware("/firmware", agent)
    version = fw_proxy.getVersion()
    return {
        "ip": ip,
        "result": "checked",
        "version": version,
    }


def update_one(
    ip,
    password,
    image_bytes,
    image_path,
    timeout,
    verify_cert,
    dry_run,
    allow_same_version,
    poll_interval,
    update_timeout,
    availability_timeout,
):
    agent = test_login(ip, password, timeout, verify_cert)
    fw_proxy = firmware.Firmware("/firmware", agent)
    current_version = fw_proxy.getVersion()

    if dry_run:
        return {
            "ip": ip,
            "result": "would_update",
            "current": current_version,
            "image": image_path,
        }

    log.info("%s: current firmware=%s; uploading image=%s", ip, current_version, image_path)
    firmware.upload(agent, image_bytes)

    image_present, image_info = fw_proxy.getImageInfo()
    if not image_present:
        raise RuntimeError("firmware upload failed: no image is present after upload")

    image_valid = getattr(image_info, "valid", None)
    image_compatible = getattr(image_info, "compatible", None)
    image_version = getattr(image_info, "version", None)
    log.info("%s: uploaded image info - %s", ip, image_info_summary(image_info))

    if image_valid is not True:
        try:
            fw_proxy.discardImage()
        except Exception as e:
            log.debug("%s: failed to discard invalid image: %s", ip, e)
        raise RuntimeError(f"uploaded firmware image is not valid: {image_info_summary(image_info)}")

    if image_compatible is not True:
        try:
            fw_proxy.discardImage()
        except Exception as e:
            log.debug("%s: failed to discard incompatible image: %s", ip, e)
        raise RuntimeError(f"uploaded firmware image is not compatible with this device: {image_info_summary(image_info)}")

    cmp_result = compare_versions(current_version, image_version)
    same_version = (
        cmp_result == 0
        or (image_version is not None and str(current_version) == str(image_version))
    )
    if same_version and not allow_same_version:
        try:
            fw_proxy.discardImage()
        except Exception as e:
            log.debug("%s: failed to discard same-version image: %s", ip, e)
        return {
            "ip": ip,
            "result": "skipped_same_version",
            "current": current_version,
            "image_version": image_version,
        }

    if cmp_result == 1:
        try:
            fw_proxy.discardImage()
        except Exception as e:
            log.debug("%s: failed to discard downgrade-looking image: %s", ip, e)
        raise RuntimeError(
            "uploaded firmware image appears older than installed firmware; "
            f"current={current_version}; image_version={image_version}. "
            "The image was discarded. Downgrades are not automated by this script."
        )

    if cmp_result is None:
        log.warning(
            "%s: could not compare current firmware '%s' to image version '%s'; proceeding because image is marked valid and compatible",
            ip,
            current_version,
            image_version,
        )

    log.info("%s: starting firmware update from %s to %s", ip, current_version, image_version)
    fw_proxy.startUpdate([])

    update_result = poll_update(
        ip=ip,
        agent=agent,
        fw_proxy=fw_proxy,
        image_version=image_version,
        poll_interval=poll_interval,
        update_timeout=update_timeout,
    )

    if update_result != "success":
        raise RuntimeError(f"firmware update did not report success: {update_result}")

    new_version = wait_for_version(
        ip=ip,
        password=password,
        verify_cert=verify_cert,
        timeout=timeout,
        wait_timeout=availability_timeout,
        expected_version=image_version,
    )

    return {
        "ip": ip,
        "result": "changed",
        "old": current_version,
        "new": new_version,
        "image_version": image_version,
    }


def process_one(ip, mode, password, args, image_bytes=None):
    try:
        if mode == "check":
            return check_one(
                ip=ip,
                password=password,
                timeout=args.timeout,
                verify_cert=not args.insecure,
            ), None

        if mode == "update":
            return update_one(
                ip=ip,
                password=password,
                image_bytes=image_bytes,
                image_path=args.image,
                timeout=args.timeout,
                verify_cert=not args.insecure,
                dry_run=args.dry_run,
                allow_same_version=args.allow_same_version,
                poll_interval=args.poll_interval,
                update_timeout=args.update_timeout,
                availability_timeout=args.availability_timeout,
            ), None

        raise RuntimeError(f"unknown mode: {mode}")
    except Exception as e:
        return {"ip": ip}, e


def parse_args():
    parser = argparse.ArgumentParser(
        description="Check or update Raritan PDU firmware for a list of IPs."
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", "-check", action="store_true", help="Check current firmware only.")
    mode.add_argument("--update", "-update", action="store_true", help="Upload and install firmware image.")

    parser.add_argument(
        "--ips",
        default="pdus.txt",
        help="File with one PDU IP address per line. Default: pdus.txt",
    )
    parser.add_argument(
        "--image",
        default=None,
        help="Firmware image file to upload. Required with --update.",
    )
    parser.add_argument(
        "--password",
        default=None,
        help=(
            "Admin password. If omitted, read from the "
            f"{DEFAULT_PASSWORD_ENV} environment variable, or prompt interactively. "
            "Avoid passing this on the command line because it may be visible in "
            "shell history and process listings."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Connection timeout in seconds for normal API calls. Default: 10",
    )
    parser.add_argument(
        "--update-timeout",
        type=int,
        default=1800,
        help="Maximum seconds to wait for firmware update status. Default: 1800",
    )
    parser.add_argument(
        "--availability-timeout",
        type=int,
        default=600,
        help="Maximum seconds to wait for the PDU to answer after update. Default: 600",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=10,
        help="Seconds between firmware update status polls. Default: 10",
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
        help="For --update, show what would happen but do not upload or start firmware update.",
    )
    parser.add_argument(
        "--allow-same-version",
        action="store_true",
        help="Allow updating even when the image version appears equal to the installed version.",
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

    if name == "checked":
        log.info("%s: OK - firmware=%s", ip, result.get("version"))
    elif name == "would_update":
        log.info(
            "%s: OK - would_update; current=%s; image=%s",
            ip,
            result.get("current"),
            result.get("image"),
        )
    elif name == "changed":
        log.info(
            "%s: OK - changed; old=%s; image_version=%s; new=%s",
            ip,
            result.get("old"),
            result.get("image_version"),
            result.get("new"),
        )
    elif name == "skipped_same_version":
        log.info(
            "%s: OK - skipped_same_version; current=%s; image_version=%s",
            ip,
            result.get("current"),
            result.get("image_version"),
        )
    else:
        log.info("%s: OK - %s", ip, name)


def main():
    args = parse_args()
    setup_logging(args.verbose, args.log_file)

    if args.concurrency < 1:
        log.error("Concurrency must be at least 1.")
        sys.exit(1)
    if args.poll_interval < 1:
        log.error("Poll interval must be at least 1 second.")
        sys.exit(1)

    mode = "check" if args.check else "update"

    if mode == "update" and not args.image:
        log.error("--image is required with --update.")
        sys.exit(1)
    if mode == "check" and args.dry_run:
        log.warning("--dry-run has no effect with --check; check mode is read-only.")

    image_bytes = None
    if mode == "update":
        if not os.path.isfile(args.image):
            log.error("Firmware image not found: %s", args.image)
            sys.exit(1)
        if not args.dry_run:
            with open(args.image, "rb") as f:
                image_bytes = f.read()
            if not image_bytes:
                log.error("Firmware image is empty: %s", args.image)
                sys.exit(1)

    ips = read_ips(args.ips)
    if not ips:
        log.error("No IPs found in %s", args.ips)
        sys.exit(1)

    password = args.password or os.environ.get(DEFAULT_PASSWORD_ENV)
    if not password:
        password = getpass.getpass(
            f"Admin password (or set ${DEFAULT_PASSWORD_ENV}): "
        )

    mode_label = "CHECK" if mode == "check" else ("UPDATE DRY RUN" if args.dry_run else "UPDATE LIVE")
    log.info(
        "Processing %d PDU(s) [%s, concurrency=%d, verify_cert=%s]...",
        len(ips),
        mode_label,
        args.concurrency,
        not args.insecure,
    )

    ok = 0
    failed = 0
    failed_ips = []
    interrupted = False

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(process_one, ip, mode, password, args, image_bytes): ip
            for ip in ips
        }
        try:
            for future in as_completed(futures):
                ip = futures[future]
                result, exc = future.result()
                if exc is not None:
                    failed += 1
                    failed_ips.append(ip)
                    log.error("%s: ERROR - %s", ip, exc)
                else:
                    ok += 1
                    log_success(result)
        except KeyboardInterrupt:
            interrupted = True
            log.warning(
                "Interrupted -- cancelling PDUs that haven't started yet. "
                "If any PDU already called startUpdate(), its firmware update "
                "is still running on the device and will NOT be stopped by this; "
                "this only stops the script from monitoring it. Check the "
                "affected PDU(s) directly before assuming anything about their state."
            )
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
