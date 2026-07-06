#!/usr/bin/env python3
"""
Configure Raritan PDU RADIUS authentication servers and local proxy user
accounts (with SNMPv3 settings) from a shared config file, across a list of
PDU IP addresses.

This does not change the active authentication method or login policy
(that's auth.AuthManager's policy/order setting, deliberately out of scope
here). It is intended as a staging step before switching authentication
order, but it still changes RADIUS server entries and creates local
accounts -- those are real changes. Use --dry-run first and test on a
small target list before running against a full fleet.

Verified resource IDs (RIDs) -- i.e. the "target" path for each interface,
confirmed against Raritan's own "Well-Known Resource IDs" reference
(help.servertech.com/json-rpc/.../well_known_rids.html), not guessed:
    /auth/radius            -> auth.RadiusManager
    /auth/role              -> usermgmt.RoleManager
    /auth/user              -> usermgmt.UserManager
    /auth/user/<user_name>  -> usermgmt.User (used for the login check)

Config file (JSON, no secrets in it -- see "Secrets" below):
{
  "radius_servers": [
    {"server": "10.10.5.10", "auth_port": 1812, "acct_port": 1813,
     "auth_type": "PAP", "timeout": 2, "retries": 3}
  ],
  "users": [
    {"username": "SVC-RADIUS-1", "full_name": "RADIUS proxy account",
     "role": "Admin"}
  ]
}
"radius_servers" and "users" are each optional (omit or use --skip-radius /
--skip-users if you only want to manage one of the two), but at least one
must be present and enabled. Duplicate usernames or duplicate server
entries within the config are rejected before any PDU is contacted.

Secrets (never stored in the config file; supported but not recommended as
plain CLI args, since those are visible in shell history and process
listings -- prefer the environment variable or interactive prompt):
    PDU admin password        --password       / PDU_ADMIN_PASSWORD
    RADIUS shared secret       --radius-secret  / RADIUS_SHARED_SECRET
    New local users' password  --user-password  / PDU_RADIUS_USER_PASSWORD
The same shared secret is applied to every server in radius_servers, and the
same password is applied to every user in users, matching how the original
manual procedure used one shared secret and one shared "standard password"
for all entries.

A note on the RADIUS shared secret and idempotency:
    The shared secret cannot be read back from the API, so there is no way
    to structurally confirm it's still correct on a PDU that already has
    RADIUS servers configured. Rather than skip the write when the visible
    fields (IP, ports, auth type) already look right -- which could leave a
    wrong or stale secret in place while reporting success -- this script
    always calls setRadiusServers() in live mode whenever radius_servers
    are enabled. That guarantees the secret is always current, at the cost
    of losing an "already configured, nothing to do" distinction, which
    was never trustworthy for this field anyway.

Common examples:
    python .\\bootstrap_pdu_radius.py --ips .\\pdus.txt --config .\\radius_config.json --dry-run -v
    python .\\bootstrap_pdu_radius.py --ips .\\pdus.txt --config .\\radius_config.json -v --log-file .\\pdu-radius.log
"""
import argparse
import getpass
import ipaddress
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import raritan.rpc
from raritan.rpc import auth, usermgmt

ADMIN_PASSWORD_ENV = "PDU_ADMIN_PASSWORD"
RADIUS_SECRET_ENV = "RADIUS_SHARED_SECRET"
USER_PASSWORD_ENV = "PDU_RADIUS_USER_PASSWORD"

_RADIUS_TARGET = "/auth/radius"
_ROLE_TARGET = "/auth/role"
_USER_MANAGER_TARGET = "/auth/user"

log = logging.getLogger("pdu_radius")

_AUTH_TYPE_MAP = {
    "PAP": auth.RadiusManager.AuthType.PAP,
    "CHAP": auth.RadiusManager.AuthType.CHAP,
    "MSCHAPV2": auth.RadiusManager.AuthType.MSCHAPv2,
}

# SNMPv3 defaults matching the original manual procedure: authentication and
# privacy passphrases both cascade from the account's own login password
# (usePasswordAsAuthPassphrase / useAuthPassphraseAsPrivPassphrase), same as
# the "Same as user password" / "Same as authentication password" checkboxes
# in the manual UI. Adjust here if your organization needs different
# protocols; this isn't exposed as a CLI flag to keep the surface area small.
_SNMPV3_SEC_LEVEL = usermgmt.SnmpV3SecLevel.AUTH_PRIV
_SNMPV3_AUTH_PROTOCOL = usermgmt.SnmpV3AuthProto.SHA1
_SNMPV3_PRIV_PROTOCOL = usermgmt.SnmpV3PrivProto.AES128

# createAccountFull()'s return codes, per Raritan's usermgmt::UserManager
# interface reference. Not all UserManager.ERR_* constants apply to this
# call the same way (some codes are contextually overloaded across
# different methods), so this map is specific to createAccountFull.
_CREATE_ACCOUNT_ERRORS = {
    1: "user_already_exists",
    2: "max_users_reached",
    3: "password_too_short_for_snmp",
    4: "invalid_user_info",
    5: "password_empty",
    6: "password_too_short",
    7: "password_too_long",
    8: "password_has_control_chars",
    9: "password_needs_lowercase",
    10: "password_needs_uppercase",
    11: "password_needs_numeric",
    12: "password_needs_special",
    14: "ssh_pubkey_too_large",
    15: "ssh_pubkey_invalid",
    16: "ssh_pubkey_not_supported",
    17: "ssh_rsa_pubkey_too_short",
    18: "username_invalid",
}

_RADIUS_OK_RESULTS = {"would_set", "set", "skipped"}
_USER_OK_RESULTS = {"already_exists", "would_create", "created", "skipped"}


def read_ips(path):
    """Read one IP per line from path.

    Blank lines are ignored. Full-line comments are ignored. Inline
    comments after '#' are allowed. Duplicate IPs are skipped. Invalid IPs
    fail loudly with the line number.
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


def load_config(path, need_radius, need_users):
    """Load the JSON config file describing RADIUS servers and local users.

    Only structural data lives here -- no secrets. Returns (radius_servers,
    users), each a possibly-empty list depending on what --skip-radius /
    --skip-users excluded.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    radius_servers = data.get("radius_servers", []) if need_radius else []
    users = data.get("users", []) if need_users else []

    if need_radius and not radius_servers:
        raise ValueError(f"{path}: no radius_servers defined (or --skip-radius was not used)")
    if need_users and not users:
        raise ValueError(f"{path}: no users defined (or --skip-users was not used)")
    if not radius_servers and not users:
        raise ValueError(f"{path}: nothing to do -- no radius_servers or users, and/or both were skipped")

    seen_servers = set()
    for i, server in enumerate(radius_servers):
        if "server" not in server:
            raise ValueError(f"{path}: radius_servers[{i}] is missing required 'server' field")
        auth_type = server.get("auth_type", "PAP").upper()
        if auth_type not in _AUTH_TYPE_MAP:
            raise ValueError(
                f"{path}: radius_servers[{i}] has unknown auth_type '{auth_type}'; "
                f"expected one of {sorted(_AUTH_TYPE_MAP)}"
            )
        if server["server"] in seen_servers:
            raise ValueError(f"{path}: duplicate radius server '{server['server']}' in radius_servers")
        seen_servers.add(server["server"])

    seen_usernames = set()
    for i, user in enumerate(users):
        if "username" not in user:
            raise ValueError(f"{path}: users[{i}] is missing required 'username' field")
        if user["username"] in seen_usernames:
            raise ValueError(f"{path}: duplicate username '{user['username']}' in users")
        seen_usernames.add(user["username"])

    return radius_servers, users


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
    """Use a small authenticated call to verify login and get a usable agent.

    Do not use pdumodel.Pdu(...).getMetaData() here. On at least one tested
    PDU/API/library combination, that call failed with KeyError:
    'supportedInletWirings'. The user API is enough to prove auth works.
    """
    agent = make_agent(ip, username, password, timeout, verify_cert)
    usermgmt.User(f"/auth/user/{username}", agent).getInfo()
    return agent


def build_desired_radius_settings(radius_servers_cfg, shared_secret):
    desired = []
    for s in radius_servers_cfg:
        auth_type = _AUTH_TYPE_MAP[s.get("auth_type", "PAP").upper()]
        desired.append(
            auth.RadiusManager.ServerSettings(
                server=s["server"],
                sharedSecret=shared_secret,
                udpAuthPort=s.get("auth_port", 1812),
                udpAccountPort=s.get("acct_port", 1813),
                timeout=s.get("timeout", 2),
                retries=s.get("retries", 3),
                authType=auth_type,
            )
        )
    return desired


def radius_lists_match(current, desired):
    """Structural comparison only. The shared secret is intentionally not
    compared -- RADIUS server config, like SNMPv3 passphrases elsewhere in
    this API, shouldn't be assumed readable-back reliably, so a matching
    secret can never be confirmed this way, only a mismatch in the fields
    that clearly can be compared.
    """
    if len(current) != len(desired):
        return False
    for cur, des in zip(current, desired):
        if (
            cur.server != des.server
            or cur.udpAuthPort != des.udpAuthPort
            or cur.udpAccountPort != des.udpAccountPort
            or cur.timeout != des.timeout
            or cur.retries != des.retries
            or cur.authType != des.authType
        ):
            return False
    return True


def configure_radius(agent, radius_servers_cfg, shared_secret, dry_run):
    radius_mgr = auth.RadiusManager(_RADIUS_TARGET, agent)
    desired = build_desired_radius_settings(radius_servers_cfg, shared_secret)

    current = radius_mgr.getRadiusServers()
    fields_already_match = radius_lists_match(current, desired)

    if dry_run:
        # Informational only -- this does not gate whether a live run will
        # write. See the module docstring's note on why the secret can't be
        # trusted to skip a write, only used to describe what dry-run saw.
        note = "visible fields already match; secret cannot be verified and will still be reapplied" if fields_already_match else None
        result = {"result": "would_set", "server_count": len(desired)}
        if note:
            result["note"] = note
        return result

    # Always write when RADIUS is enabled for this run, even if the visible
    # fields already match -- see the module docstring for why skipping the
    # write here would risk leaving a wrong/stale shared secret in place
    # while reporting success.
    ret = radius_mgr.setRadiusServers(desired)
    if ret != 0:
        return {"result": f"setRadiusServers_failed_ret_{ret}", "server_count": len(desired)}

    verify = radius_mgr.getRadiusServers()
    if not radius_lists_match(verify, desired):
        return {"result": "verify_failed", "server_count": len(desired)}

    return {"result": "set", "server_count": len(desired)}


def get_role_id_map(agent):
    role_mgr = usermgmt.RoleManager(_ROLE_TARGET, agent)
    roles = role_mgr.getAllRoles()
    return {r.name: r.id for r in roles}


def configure_user(agent, user_cfg, password, dry_run, existing_names, role_id_map):
    username = user_cfg["username"]

    if username in existing_names:
        return {"username": username, "result": "already_exists"}

    # Resolve the role before the dry-run check, not after, so a typo'd
    # role name (e.g. "Adminn") is caught during dry-run as role_not_found
    # instead of dry-run reporting would_create and only failing later
    # during the live run.
    role_ids = []
    role_name = user_cfg.get("role")
    if role_name:
        role_id = role_id_map.get(role_name)
        if role_id is None:
            return {"username": username, "result": f"role_not_found:{role_name}"}
        role_ids = [role_id]

    if dry_run:
        return {"username": username, "result": "would_create"}

    snmp_settings = usermgmt.SnmpV3Settings(
        enabled=True,
        secLevel=_SNMPV3_SEC_LEVEL,
        authProtocol=_SNMPV3_AUTH_PROTOCOL,
        usePasswordAsAuthPassphrase=True,
        privProtocol=_SNMPV3_PRIV_PROTOCOL,
        useAuthPassphraseAsPrivPassphrase=True,
    )
    info = usermgmt.UserInfo(
        enabled=True,
        needPasswordChange=False,
        auxInfo=usermgmt.AuxInfo(fullname=user_cfg.get("full_name", "")),
        snmpV3Settings=snmp_settings,
        roleIds=role_ids,
    )

    user_mgr = usermgmt.UserManager(_USER_MANAGER_TARGET, agent)
    ret = user_mgr.createAccountFull(username, password, info)
    if ret != 0:
        return {"username": username, "result": _CREATE_ACCOUNT_ERRORS.get(ret, f"unknown_create_error_{ret}")}

    # Verify the account actually exists afterward, same as the other
    # scripts verify a write by reading it back, rather than trusting a
    # 0 return code alone.
    names_after = user_mgr.getAccountNames()
    if username not in names_after:
        return {"username": username, "result": "verify_failed_not_found_after_create"}

    return {"username": username, "result": "created"}


def configure_pdu(
    ip,
    admin_username,
    admin_password,
    radius_secret,
    user_password,
    radius_servers_cfg,
    users_cfg,
    timeout,
    verify_cert,
    dry_run,
):
    agent = test_login(ip, admin_username, admin_password, timeout, verify_cert)

    result = {"ip": ip, "radius": None, "users": []}

    if radius_servers_cfg:
        result["radius"] = configure_radius(agent, radius_servers_cfg, radius_secret, dry_run)

    if users_cfg:
        existing_names = usermgmt.UserManager(_USER_MANAGER_TARGET, agent).getAccountNames()
        role_id_map = None
        if any(u.get("role") for u in users_cfg):
            role_id_map = get_role_id_map(agent)

        for user_cfg in users_cfg:
            user_result = configure_user(
                agent, user_cfg, user_password, dry_run, existing_names, role_id_map or {}
            )
            result["users"].append(user_result)

    return result


def process_one(
    ip,
    admin_username,
    admin_password,
    radius_secret,
    user_password,
    radius_servers_cfg,
    users_cfg,
    args,
):
    try:
        result = configure_pdu(
            ip=ip,
            admin_username=admin_username,
            admin_password=admin_password,
            radius_secret=radius_secret,
            user_password=user_password,
            radius_servers_cfg=radius_servers_cfg,
            users_cfg=users_cfg,
            timeout=args.timeout,
            verify_cert=not args.insecure,
            dry_run=args.dry_run,
        )
        return result, None
    except Exception as e:
        return {"ip": ip}, e


def ip_succeeded(result):
    radius = result.get("radius")
    if radius is not None and radius["result"] not in _RADIUS_OK_RESULTS:
        return False
    for user_result in result.get("users", []):
        if user_result["result"] not in _USER_OK_RESULTS:
            return False
    return True


def log_result(result):
    ip = result["ip"]
    radius = result.get("radius")
    if radius is not None:
        level = log.info if radius["result"] in _RADIUS_OK_RESULTS else log.error
        if radius.get("note"):
            level(
                "%s: RADIUS - %s; servers=%d; note=%s",
                ip,
                radius["result"],
                radius["server_count"],
                radius["note"],
            )
        else:
            level(
                "%s: RADIUS - %s; servers=%d",
                ip,
                radius["result"],
                radius["server_count"],
            )
    for user_result in result.get("users", []):
        level = log.info if user_result["result"] in _USER_OK_RESULTS else log.error
        level(
            "%s: USER %s - %s",
            ip,
            user_result["username"],
            user_result["result"],
        )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Configure Raritan PDU RADIUS servers and local proxy users from a config file."
    )
    parser.add_argument("--ips", default="pdus.txt", help="File with one PDU IP per line. Default: pdus.txt")
    parser.add_argument(
        "--config",
        default="radius_config.json",
        help="JSON config file describing radius_servers and/or users. Default: radius_config.json",
    )
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
        "--radius-secret",
        default=None,
        help=(
            "RADIUS shared secret, applied to every server in the config. If omitted, "
            f"read from the {RADIUS_SECRET_ENV} environment variable, or prompted "
            "interactively. Avoid passing this on the command line."
        ),
    )
    parser.add_argument(
        "--user-password",
        default=None,
        help=(
            "Password for every new local user in the config. If omitted, read from the "
            f"{USER_PASSWORD_ENV} environment variable, or prompted interactively. "
            "Avoid passing this on the command line."
        ),
    )
    parser.add_argument("--skip-radius", action="store_true", help="Don't touch RADIUS server configuration.")
    parser.add_argument("--skip-users", action="store_true", help="Don't create any local user accounts.")
    parser.add_argument("--timeout", type=int, default=10, help="Connection timeout in seconds. Default: 10")
    parser.add_argument(
        "--concurrency", type=int, default=1, help="Number of PDUs to process in parallel. Default: 1"
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=True,
        help="Disable TLS certificate verification (default: on). Pass --no-insecure to require valid certificates.",
    )
    parser.add_argument(
        "--no-insecure", action="store_false", dest="insecure", help="Require valid TLS certificates."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change; do not write RADIUS config or create any users.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    parser.add_argument("--log-file", default=None, help="Optional path to also write logs to a file.")
    return parser.parse_args()


def setup_logging(verbose, log_file):
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
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

    if args.skip_radius and args.skip_users:
        log.error("Both --skip-radius and --skip-users were given; nothing to do.")
        sys.exit(1)

    try:
        ips = read_ips(args.ips)
    except Exception as e:
        log.error("%s", e)
        sys.exit(1)

    if not ips:
        log.error("No IPs found in %s", args.ips)
        sys.exit(1)

    try:
        radius_servers_cfg, users_cfg = load_config(
            args.config, need_radius=not args.skip_radius, need_users=not args.skip_users
        )
    except Exception as e:
        log.error("%s", e)
        sys.exit(1)

    admin_password = args.password or os.environ.get(ADMIN_PASSWORD_ENV)
    if not admin_password:
        admin_password = getpass.getpass(f"PDU admin password (or set ${ADMIN_PASSWORD_ENV}): ")

    # Dry-run always needs the admin password (it still logs in and reads
    # current state), but it never writes RADIUS settings and never
    # compares the shared secret, and it never creates a user -- so there's
    # nothing for the RADIUS secret or the new user password to do in
    # dry-run. Skip prompting for either.
    if args.dry_run:
        radius_secret = "unused-in-dry-run"
        user_password = "unused-in-dry-run"
    else:
        radius_secret = None
        if radius_servers_cfg:
            radius_secret = args.radius_secret or os.environ.get(RADIUS_SECRET_ENV)
            if not radius_secret:
                # Only confirm when typed interactively -- env var / --flag
                # values are trusted as-is, same as every other secret in
                # this project. A mistyped secret here would still be
                # accepted by setRadiusServers() and can't be verified
                # afterward, so a typo here is worth catching up front.
                radius_secret = getpass.getpass(f"RADIUS shared secret (or set ${RADIUS_SECRET_ENV}): ")
                radius_secret_confirm = getpass.getpass("Confirm RADIUS shared secret: ")
                if radius_secret != radius_secret_confirm:
                    log.error("RADIUS shared secret and confirmation do not match.")
                    sys.exit(1)

        user_password = None
        if users_cfg:
            user_password = args.user_password or os.environ.get(USER_PASSWORD_ENV)
            if not user_password:
                user_password = getpass.getpass(f"New user account password (or set ${USER_PASSWORD_ENV}): ")
                user_password_confirm = getpass.getpass("Confirm new user account password: ")
                if user_password != user_password_confirm:
                    log.error("New user account password and confirmation do not match.")
                    sys.exit(1)

    mode = "DRY RUN" if args.dry_run else "LIVE"
    log.info(
        "Processing %d PDU(s) [%s, concurrency=%d, verify_cert=%s]...",
        len(ips),
        mode,
        args.concurrency,
        not args.insecure,
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
                args.username,
                admin_password,
                radius_secret,
                user_password,
                radius_servers_cfg,
                users_cfg,
                args,
            ): ip
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
                    continue

                log_result(result)
                if ip_succeeded(result):
                    ok += 1
                else:
                    failed += 1
                    failed_ips.append(ip)

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
