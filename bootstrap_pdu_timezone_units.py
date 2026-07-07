#!/usr/bin/env python3
r"""
Configure Raritan PX-series PDU timezone and display units across a list of
PDU management IP addresses.

Default behavior:
    * Set device timezone to America/Chicago, which is Central Time with DST
      handled by the timezone database when the PDU supports DST info.
    * Set default display preferences to Fahrenheit and feet.
    * Set every existing local user's display preferences to Fahrenheit and feet.

This script does not change power state, outlets, thresholds, network settings,
RADIUS settings, or passwords.

Common examples:
    python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --dry-run -v
    python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt -v --log-file .\pdu-timezone-units.log
    python .\bootstrap_pdu_timezone_units.py --ips .\pdus.txt --list-timezones

Use --dry-run first.
"""

import argparse
import copy
import getpass
import ipaddress
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import raritan.rpc
from raritan.rpc import datetime as rpc_datetime
from raritan.rpc import usermgmt

ADMIN_PASSWORD_ENV = "PDU_ADMIN_PASSWORD"

_DATETIME_TARGET = "/datetime"
_USER_MANAGER_TARGET = "/auth/user"
_CURRENT_USER_TARGET = "/auth/currentUser"

_DEFAULT_TIMEZONE = "America/Chicago"

log = logging.getLogger("pdu_timezone_units")

_TIMEZONE_OK_RESULTS = {"already_configured", "would_set", "set"}
_PREF_OK_RESULTS = {"already_configured", "would_set", "set", "skipped_not_modifyable"}


def enum_value(enum_class_name, member_name):
    """Return an enum value from generated raritan bindings.

    The generated bindings normally expose nested enum classes like
    usermgmt.TemperatureEnum.DEG_F. This helper also supports older bindings
    that may expose enum members directly under usermgmt.
    """
    enum_class = getattr(usermgmt, enum_class_name, None)
    if enum_class is not None and hasattr(enum_class, member_name):
        return getattr(enum_class, member_name)
    if hasattr(usermgmt, member_name):
        return getattr(usermgmt, member_name)
    raise RuntimeError(f"raritan.usermgmt is missing {enum_class_name}.{member_name}")


TEMP_F = enum_value("TemperatureEnum", "DEG_F")
LENGTH_FEET = enum_value("LengthEnum", "FEET")


def enum_text(value):
    return getattr(value, "name", str(value))


def read_ips(path):
    """Read one PDU management IP per line.

    Blank lines are ignored. Full-line comments are ignored. Inline comments
    after '#' are allowed. Duplicate IPs are skipped. Invalid IPs fail loudly
    with the line number.
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
    """Create an authenticated agent and verify it with a small user call."""
    agent = make_agent(ip, username, password, timeout, verify_cert)

    try:
        usermgmt.User(_CURRENT_USER_TARGET, agent).getInfo()
    except Exception:
        # Older devices/bindings may not expose /auth/currentUser consistently.
        # Fall back to the explicit user RID used by the other PDU scripts.
        usermgmt.User(f"{_USER_MANAGER_TARGET}/{username}", agent).getInfo()

    return agent


def timezone_search_terms(timezone_name):
    requested = timezone_name.strip()
    lowered = requested.lower()

    if lowered in {"cst", "cdt", "central", "central time", "us/central"}:
        return [
            "America/Chicago",
            "US/Central",
            "Chicago",
            "Central Time",
            "Central Standard Time",
        ]

    terms = [requested]
    if requested == _DEFAULT_TIMEZONE:
        terms.extend(["US/Central", "Chicago", "Central Time", "Central Standard Time"])
    return terms


def find_timezone(datetime_proxy, timezone_name):
    """Find a supported time zone, preferring the id-based approach.

    Raritan's own ZoneCfg documentation states there are two ways to
    identify a time zone: id-based (id != 0, name is just a display label)
    and name-based/Olson (id == 0, name is the IANA name). It explicitly
    warns that "the user frontends currently only support the id-based
    approach and may not work correctly with the name-based approach" --
    confirmed visually against this PDU's own Date/Time page, which only
    ever shows id-based labels like "(UTC-05:00) Eastern Time (US &
    Canada)", never an IANA-style name. So id-based zones are searched
    first here, and Olson/name-based zones are only used as a fallback if
    no id-based match exists.
    """
    terms = timezone_search_terms(timezone_name)

    for use_olson in (False, True):
        zones = datetime_proxy.getZoneInfos(use_olson)

        for term in terms:
            for zone in zones:
                if str(zone.name).lower() == term.lower():
                    return zone, use_olson

        for term in terms:
            for zone in zones:
                if term.lower() in str(zone.name).lower():
                    return zone, use_olson

    raise ValueError(
        f"timezone not found on this PDU: {timezone_name}. "
        "Run with --list-timezones against one PDU to see supported names."
    )


def zone_summary(zone):
    return f"id={zone.id}, name={zone.name}, hasDSTInfo={zone.hasDSTInfo}"


def configure_timezone(agent, timezone_name, enable_auto_dst, dry_run):
    datetime_proxy = rpc_datetime.DateTime(_DATETIME_TARGET, agent)
    cfg = datetime_proxy.getCfg()
    desired_zone, used_olson = find_timezone(datetime_proxy, timezone_name)

    desired_dst = bool(enable_auto_dst and getattr(desired_zone, "hasDSTInfo", False))
    current_zone_cfg = cfg.zoneCfg
    current_id = getattr(current_zone_cfg, "id", None)
    current_name = getattr(current_zone_cfg, "name", "")
    current_dst = getattr(current_zone_cfg, "enableAutoDST", None)

    current = f"id={current_id}, name={current_name}, autoDST={current_dst}"
    desired = f"{zone_summary(desired_zone)}, autoDST={desired_dst}"

    id_matches = getattr(desired_zone, "id", 0) != 0 and current_id == desired_zone.id
    name_matches = getattr(desired_zone, "id", 0) == 0 and str(current_name).lower() == str(desired_zone.name).lower()
    dst_matches = current_dst == desired_dst

    if (id_matches or name_matches) and dst_matches:
        return {
            "result": "already_configured",
            "current": current,
            "desired": desired,
            "used_olson": used_olson,
        }

    if dry_run:
        return {
            "result": "would_set",
            "current": current,
            "desired": desired,
            "used_olson": used_olson,
        }

    cfg.zoneCfg.id = desired_zone.id
    cfg.zoneCfg.name = desired_zone.name
    cfg.zoneCfg.enableAutoDST = desired_dst

    ret = datetime_proxy.setCfg(cfg)
    if ret != 0:
        return {
            "result": f"setCfg_failed_ret_{ret}",
            "current": current,
            "desired": desired,
            "used_olson": used_olson,
        }

    cfg_after = datetime_proxy.getCfg()
    after = cfg_after.zoneCfg
    after_id = getattr(after, "id", None)
    after_name = getattr(after, "name", "")
    after_dst = getattr(after, "enableAutoDST", None)

    id_verified = desired_zone.id != 0 and after_id == desired_zone.id
    name_verified = desired_zone.id == 0 and str(after_name).lower() == str(desired_zone.name).lower()
    dst_verified = after_dst == desired_dst

    if (id_verified or name_verified) and dst_verified:
        return {
            "result": "set",
            "current": current,
            "desired": desired,
            "used_olson": used_olson,
        }

    return {
        "result": "verify_failed",
        "current": current,
        "desired": desired,
        "after": f"id={after_id}, name={after_name}, autoDST={after_dst}",
        "used_olson": used_olson,
    }


def preferences_match(prefs):
    return prefs.temperatureUnit == TEMP_F and prefs.lengthUnit == LENGTH_FEET


def desired_preferences_from(prefs):
    desired = copy.deepcopy(prefs)
    desired.temperatureUnit = TEMP_F
    desired.lengthUnit = LENGTH_FEET
    # Leave pressureUnit unchanged because the requested change was only
    # temperature to Fahrenheit and distance/length to feet.
    return desired


def prefs_summary(prefs):
    pressure = getattr(prefs, "pressureUnit", None)
    pressure_text = f", pressure={enum_text(pressure)}" if pressure is not None else ""
    return f"temp={enum_text(prefs.temperatureUnit)}, length={enum_text(prefs.lengthUnit)}{pressure_text}"


def configure_default_preferences(agent, dry_run):
    user_mgr = usermgmt.UserManager(_USER_MANAGER_TARGET, agent)
    current = user_mgr.getDefaultPreferences()
    desired = desired_preferences_from(current)

    if preferences_match(current):
        return {
            "result": "already_configured",
            "current": prefs_summary(current),
            "desired": prefs_summary(desired),
        }

    if dry_run:
        return {
            "result": "would_set",
            "current": prefs_summary(current),
            "desired": prefs_summary(desired),
        }

    ret = user_mgr.setDefaultPreferences(desired)
    if ret != 0:
        return {
            "result": f"setDefaultPreferences_failed_ret_{ret}",
            "current": prefs_summary(current),
            "desired": prefs_summary(desired),
        }

    after = user_mgr.getDefaultPreferences()
    if preferences_match(after):
        return {
            "result": "set",
            "current": prefs_summary(current),
            "desired": prefs_summary(desired),
        }

    return {
        "result": "verify_failed",
        "current": prefs_summary(current),
        "desired": prefs_summary(desired),
        "after": prefs_summary(after),
    }


def configure_user_preferences(agent, username, dry_run):
    user = usermgmt.User(f"{_USER_MANAGER_TARGET}/{username}", agent)

    try:
        caps = user.getCapabilities()
        if hasattr(caps, "canSetPreferences") and not caps.canSetPreferences:
            return {
                "username": username,
                "result": "skipped_not_modifyable",
                "current": "capability canSetPreferences=False",
                "desired": f"temp={enum_text(TEMP_F)}, length={enum_text(LENGTH_FEET)}",
            }
    except Exception as e:
        log.debug("%s: could not read capabilities for user %s: %s", _USER_MANAGER_TARGET, username, e)

    info = user.getInfo()
    current = info.preferences
    desired = desired_preferences_from(current)

    if preferences_match(current):
        return {
            "username": username,
            "result": "already_configured",
            "current": prefs_summary(current),
            "desired": prefs_summary(desired),
        }

    if dry_run:
        return {
            "username": username,
            "result": "would_set",
            "current": prefs_summary(current),
            "desired": prefs_summary(desired),
        }

    ret = user.setPreferences(desired)
    if ret != 0:
        return {
            "username": username,
            "result": f"setPreferences_failed_ret_{ret}",
            "current": prefs_summary(current),
            "desired": prefs_summary(desired),
        }

    after = user.getInfo().preferences
    if preferences_match(after):
        return {
            "username": username,
            "result": "set",
            "current": prefs_summary(current),
            "desired": prefs_summary(desired),
        }

    return {
        "username": username,
        "result": "verify_failed",
        "current": prefs_summary(current),
        "desired": prefs_summary(desired),
        "after": prefs_summary(after),
    }


def list_timezones(ip, username, password, timeout, verify_cert):
    agent = test_login(ip, username, password, timeout, verify_cert)
    datetime_proxy = rpc_datetime.DateTime(_DATETIME_TARGET, agent)

    print(f"Supported Olson/IANA time zones from {ip}:")
    for zone in datetime_proxy.getZoneInfos(True):
        print(f"  id={zone.id:<5} hasDSTInfo={str(zone.hasDSTInfo):<5} name={zone.name}")

    print(f"\nSupported display time zones from {ip}:")
    for zone in datetime_proxy.getZoneInfos(False):
        print(f"  id={zone.id:<5} hasDSTInfo={str(zone.hasDSTInfo):<5} name={zone.name}")


def configure_pdu(ip, admin_username, admin_password, args):
    agent = test_login(
        ip=ip,
        username=admin_username,
        password=admin_password,
        timeout=args.timeout,
        verify_cert=not args.insecure,
    )

    user_mgr = usermgmt.UserManager(_USER_MANAGER_TARGET, agent)

    result = {
        "ip": ip,
        "timezone": configure_timezone(agent, args.timezone, not args.no_auto_dst, args.dry_run),
        "default_preferences": configure_default_preferences(agent, args.dry_run),
        "users": [],
    }

    if args.apply_users:
        for username in user_mgr.getAccountNames():
            result["users"].append(configure_user_preferences(agent, username, args.dry_run))

    return result


def process_one(ip, admin_username, admin_password, args):
    try:
        result = configure_pdu(ip, admin_username, admin_password, args)
        return result, None
    except Exception as e:
        return {"ip": ip}, e


def ip_succeeded(result):
    timezone = result.get("timezone")
    if timezone is not None and timezone["result"] not in _TIMEZONE_OK_RESULTS:
        return False

    default_preferences = result.get("default_preferences")
    if default_preferences is not None and default_preferences["result"] not in _PREF_OK_RESULTS:
        return False

    for user_result in result.get("users", []):
        if user_result["result"] not in _PREF_OK_RESULTS:
            return False

    return True


def log_result(result):
    ip = result["ip"]

    timezone = result.get("timezone")
    if timezone is not None:
        level = log.info if timezone["result"] in _TIMEZONE_OK_RESULTS else log.error
        level(
            "%s: TIMEZONE - %s; current=(%s); desired=(%s)",
            ip,
            timezone["result"],
            timezone.get("current", "unknown"),
            timezone.get("desired", "unknown"),
        )
        if "after" in timezone:
            log.error("%s: TIMEZONE after=(%s)", ip, timezone["after"])

    default_preferences = result.get("default_preferences")
    if default_preferences is not None:
        level = log.info if default_preferences["result"] in _PREF_OK_RESULTS else log.error
        level(
            "%s: DEFAULT PREFS - %s; current=(%s); desired=(%s)",
            ip,
            default_preferences["result"],
            default_preferences.get("current", "unknown"),
            default_preferences.get("desired", "unknown"),
        )
        if "after" in default_preferences:
            log.error("%s: DEFAULT PREFS after=(%s)", ip, default_preferences["after"])

    for user_result in result.get("users", []):
        level = log.info if user_result["result"] in _PREF_OK_RESULTS else log.error
        level(
            "%s: USER %s PREFS - %s; current=(%s); desired=(%s)",
            ip,
            user_result["username"],
            user_result["result"],
            user_result.get("current", "unknown"),
            user_result.get("desired", "unknown"),
        )
        if "after" in user_result:
            log.error("%s: USER %s PREFS after=(%s)", ip, user_result["username"], user_result["after"])


def parse_args():
    parser = argparse.ArgumentParser(
        description="Configure Raritan PDU timezone plus Fahrenheit/feet display preferences."
    )
    parser.add_argument("--ips", default="pdus.txt", help="File with one PDU IP per line. Default: pdus.txt")
    parser.add_argument("--username", default="admin", help="PDU login username. Default: admin")
    parser.add_argument(
        "--password",
        default=None,
        help=(
            "PDU admin password. If omitted, read from the "
            f"{ADMIN_PASSWORD_ENV} environment variable, or prompted interactively. "
            "Avoid passing this on the command line."
        ),
    )
    parser.add_argument(
        "--timezone",
        default=_DEFAULT_TIMEZONE,
        help=(
            "Timezone to apply. Default: America/Chicago. "
            "Aliases CST, CDT, Central, and US/Central also resolve toward America/Chicago."
        ),
    )
    parser.add_argument(
        "--no-auto-dst",
        action="store_true",
        help="Do not enable automatic DST adjustment even if the selected zone supports it.",
    )
    parser.set_defaults(apply_users=True)
    parser.add_argument(
        "--apply-users",
        dest="apply_users",
        action="store_true",
        help="Apply Fahrenheit/feet preferences to every existing local user. Default behavior.",
    )
    parser.add_argument(
        "--no-apply-users",
        dest="apply_users",
        action="store_false",
        help="Only set default preferences; do not update existing users.",
    )
    parser.add_argument(
        "--list-timezones",
        action="store_true",
        help="List supported time zones from the first PDU in --ips and exit without making changes.",
    )
    parser.add_argument("--timeout", type=int, default=10, help="Connection timeout per PDU. Default: 10")
    parser.add_argument("--concurrency", type=int, default=1, help="Number of PDUs to process in parallel. Default: 1")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing settings.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    parser.add_argument("--log-file", default=None, help="Also write logs to this file.")
    parser.add_argument(
        "--insecure",
        dest="insecure",
        action="store_true",
        default=True,
        help="Disable HTTPS certificate verification. Default behavior.",
    )
    parser.add_argument(
        "--no-insecure",
        dest="insecure",
        action="store_false",
        help="Require valid trusted HTTPS certificates.",
    )
    return parser.parse_args()


def setup_logging(verbose, log_file):
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [logging.StreamHandler()]

    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def get_admin_password(args):
    if args.password:
        return args.password

    env_password = os.environ.get(ADMIN_PASSWORD_ENV)
    if env_password:
        return env_password

    return getpass.getpass("PDU admin password: ")


def main():
    args = parse_args()
    setup_logging(args.verbose, args.log_file)

    if args.timeout <= 0:
        log.error("--timeout must be greater than 0")
        return 1

    if args.concurrency <= 0:
        log.error("--concurrency must be greater than 0")
        return 1

    try:
        ips = read_ips(args.ips)
    except Exception as e:
        log.error("%s", e)
        return 1

    if not ips:
        log.error("No IPs found in %s", args.ips)
        return 1

    admin_password = get_admin_password(args)

    if args.list_timezones:
        try:
            list_timezones(ips[0], args.username, admin_password, args.timeout, not args.insecure)
            return 0
        except Exception as e:
            log.error("%s: ERROR - %s", ips[0], e)
            return 2

    mode = "DRY RUN" if args.dry_run else "LIVE"
    log.info(
        "Processing %d PDU(s) [%s, timezone=%s, units=Fahrenheit/feet, apply_users=%s, concurrency=%d, verify_cert=%s]...",
        len(ips),
        mode,
        args.timezone,
        args.apply_users,
        args.concurrency,
        not args.insecure,
    )

    ok = 0
    failed = []
    interrupted = False

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        future_map = {
            executor.submit(process_one, ip, args.username, admin_password, args): ip
            for ip in ips
        }

        try:
            for future in as_completed(future_map):
                ip = future_map[future]
                result, error = future.result()

                if error is not None:
                    log.error("%s: ERROR - %s", ip, error)
                    failed.append(ip)
                    continue

                log_result(result)

                if ip_succeeded(result):
                    ok += 1
                else:
                    failed.append(ip)

        except KeyboardInterrupt:
            interrupted = True
            log.warning("Interrupted -- cancelling remaining PDUs and shutting down...")
            for f in future_map:
                f.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

    if interrupted:
        log.info("Done. OK=%d Failed=%d (interrupted)", ok, len(failed))
        if failed:
            log.info("Failed IPs: %s", ", ".join(failed))
        return 130

    log.info("Done. OK=%d Failed=%d", ok, len(failed))
    if failed:
        log.info("Failed IPs: %s", ", ".join(failed))
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
