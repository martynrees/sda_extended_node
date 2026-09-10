#!/usr/bin/env python3
"""Extended-node onboarding automation for Catalyst Center (SD-Access fabric).

    python onboard_extended_nodes.py prepare --csv extended_nodes.csv [--dry-run]
    # ... rack, cable, power on the extended node(s) whenever ready ...
    python onboard_extended_nodes.py monitor --csv extended_nodes.csv

Environment-constant settings (base URLs, NDG target, SSH device type/port,
etc.) can be pre-filled from a `.env` file next to this script instead of
repeated on every command line — see README.md for the full list of
supported variables. Copy `.env.example` to `.env` to get started. CLI flags
always take precedence over `.env` values. Passwords are never read from
`.env` or any other file — always prompted interactively.

See README.md for full usage and manual prerequisites.
"""

import argparse
import csv as csv_module
import json
import os
import sys
import time
import traceback

from dotenv import load_dotenv

from lib import dnac_client, hostname_client, ise_client, port_channels
from lib.csv_loader import CsvValidationError, load_rows
from lib.resolvers import Resolver, ResolverError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")

load_dotenv(os.path.join(SCRIPT_DIR, ".env"))


def _env_bool(name):
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    value = (os.environ.get(name) or "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        print(f"WARNING: ignoring non-numeric {name}={value!r} from environment/.env", file=sys.stderr)
        return default


def _to_jsonable(obj):
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "to_dict"):
        return _to_jsonable(obj.to_dict())
    return obj


def _dump_debug(debug_dir, serial, payload):
    os.makedirs(debug_dir, exist_ok=True)
    path = os.path.join(debug_dir, f"{serial}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_to_jsonable(payload), f, indent=2, default=str)
    return path


def _write_results_csv(phase, results):
    os.makedirs(LOGS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(LOGS_DIR, f"{phase}_results_{timestamp}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv_module.DictWriter(
            f, fieldnames=["row_number", "hostname", "serial", "status", "detail"]
        )
        writer.writeheader()
        writer.writerows(results)
    return path


def _print_summary(phase, results):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    print(f"\n{'=' * 60}")
    print(f"{phase} summary: {len(results)} row(s) processed")
    for status, count in sorted(counts.items()):
        print(f"  {status:10s}: {count}")
    print(f"{'=' * 60}\n")


def _run_phase(phase_name, rows, row_handler, write_csv=True):
    """Run row_handler(row) for every row, catching per-row errors.

    row_handler must return {"status": ..., "detail": ...}. Any exception is
    caught and recorded as status="failed" so one bad row cannot abort the
    batch.
    """
    results = []
    for row in rows:
        try:
            outcome = row_handler(row)
        except (ResolverError, port_channels.PortChannelError, dnac_client.TaskError) as exc:
            outcome = {"status": "failed", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - keep the batch alive on unexpected errors
            traceback.print_exc()
            outcome = {"status": "failed", "detail": f"unexpected error: {exc}"}

        print(f"  [row {row.row_number}] {row.extended_node_hostname} ({row.extended_node_serial}): "
              f"{outcome['status']} - {outcome['detail']}")

        results.append(
            {
                "row_number": row.row_number,
                "hostname": row.extended_node_hostname,
                "serial": row.extended_node_serial,
                "status": outcome["status"],
                "detail": outcome["detail"],
            }
        )

    if write_csv:
        results_path = _write_results_csv(phase_name, results)
        print(f"\nResults written to {results_path}")
    _print_summary(phase_name, results)
    return results


def cmd_prepare(dnac, resolver, rows, args):
    """Create the fabric port channel for every row.

    The extended node is discovered and onboarded natively over LLDP/CDP
    once it's racked, cabled into this port channel, and powered on — no PnP
    pre-staging step. (Pre-claiming the device in PnP before it connects
    causes Catalyst Center to onboard it as an edge node instead of an
    extended node, so that step has been removed from this flow.)
    """
    def handler(row):
        pc_result = port_channels.create_port_channel(dnac, resolver, row, dry_run=args.dry_run)

        if args.dry_run:
            status = "dry-run"
        elif pc_result["status"] == "skipped":
            status = "skipped"
        else:
            status = "ok"

        return {"status": status, "detail": f"port-channel: {pc_result['status']} - {pc_result['detail']}"}

    _run_phase("prepare", rows, handler)


def cmd_monitor(dnac, resolver, rows, args, ise_session=None, catc_username=None, catc_password=None):
    """Report each device's current discovery / fabric-role state.

    Stateless and safe to re-run at any time, in any order — devices that
    haven't connected yet just report "not-seen"; there's no requirement to
    wait for the whole batch before checking on the ones that are ready.

    If ise_session is set, also checks/remediates the device's ISE Network
    Device Group membership as soon as the device is visible in Catalyst
    Center inventory — not gated on pending/warning/verified, since Catalyst
    Center can push TACACS config (which auto-creates the device in ISE)
    before the fabric-role query settles. The ISE outcome is appended to
    `detail` as an informational suffix; it never changes the primary
    status, which stays driven by CatC state only.

    If args.rename_hostname is set, also pushes the CSV's
    extended_node_hostname to the device over SSH (same as-soon-as-visible
    gating as ISE). Catalyst Center's own provisioning/sync process picks up
    the renamed device on its own; this does not trigger a resync itself
    (an on-demand forcesync call was tried and dropped — it conflicted with
    Catalyst Center's own in-flight provisioning/sync of the device).
    """
    debug_dir = None
    if args.debug:
        debug_dir = os.path.join(LOGS_DIR, "debug_" + time.strftime("%Y%m%d-%H%M%S"))

    def handler(row):
        debug_payload = {"device_inventory": None, "fabric_role_response": None}

        response = dnac.devices.get_device_list(serial_number=row.extended_node_serial)
        items = response.get("response") if isinstance(response, dict) else response.response
        debug_payload["device_inventory"] = items
        if not items:
            if debug_dir:
                _dump_debug(debug_dir, row.extended_node_serial, debug_payload)
            return {"status": "not-seen", "detail": "device not yet visible in Catalyst Center inventory"}

        device = items[0]
        reachability = device.get("reachabilityStatus", "unknown")
        management_ip = device.get("managementIpAddress")

        # Live hostname from CatC inventory, not the CSV column — confirmed
        # in testing that CatC (and therefore what it auto-creates in ISE)
        # names the device SN-<serial>, not row.extended_node_hostname.
        ise_note = None
        if ise_session:
            try:
                ise_device = ise_client.get_network_device_by_hostname(ise_session, device.get("hostname"))
                if ise_device is None:
                    ise_note = "ISE: network device not yet present"
                else:
                    result = ise_client.ensure_ndg_membership(
                        ise_session, ise_device, args.ise_ndg, dry_run=args.ise_dry_run
                    )
                    ise_note = f"ISE: {result['status']} - {result['detail']}"
            except ise_client.IseError as exc:
                ise_note = f"ISE: error - {exc}"

        rename_note = None
        if args.rename_hostname:
            try:
                device_record = resolver.resolve_device_by_serial(row.extended_node_serial)
                live_hostname = device_record.get("hostname") or ""
                if live_hostname.lower() == row.extended_node_hostname.lower():
                    rename_note = "rename: unchanged"
                elif not device_record.get("managementIpAddress"):
                    rename_note = "rename: error - device has no managementIpAddress"
                else:
                    result = hostname_client.push_hostname(
                        device_record["managementIpAddress"],
                        catc_username,
                        catc_password,
                        row.extended_node_hostname,
                        device_type=args.device_type,
                        port=args.device_port,
                        dry_run=args.rename_dry_run,
                    )
                    rename_note = f"rename: {result['status']} - {result['detail']}"
            except ResolverError as exc:
                rename_note = f"rename: error - {exc}"
            except hostname_client.HostnameError as exc:
                rename_note = f"rename: error - {exc}"

        def with_notes(outcome):
            if ise_note:
                outcome["detail"] += f" | {ise_note}"
            if rename_note:
                outcome["detail"] += f" | {rename_note}"
            return outcome

        try:
            role_response = port_channels.get_device_role_response(dnac, management_ip)
        except port_channels.DeviceNotProvisionedError:
            if debug_dir:
                _dump_debug(debug_dir, row.extended_node_serial, debug_payload)
            return with_notes({
                "status": "pending",
                "detail": f"reachability={reachability} -- visible in inventory but not yet "
                "provisioned/assigned to a site in Catalyst Center; still onboarding, check again shortly",
            })
        debug_payload["fabric_role_response"] = role_response
        fabric_roles = role_response.get("roles") or []

        if debug_dir:
            path = _dump_debug(debug_dir, row.extended_node_serial, debug_payload)
            print(f"    debug dump: {path}")

        if "Extended Node" not in fabric_roles:
            return with_notes({
                "status": "warning",
                "detail": f"reachability={reachability}, fabric roles={fabric_roles or 'none'} "
                "-- visible in inventory but fabric role is not 'Extended Node', check manually",
            })

        return with_notes(
            {"status": "verified", "detail": f"reachability={reachability}, fabric roles={fabric_roles}"}
        )

    if not args.watch:
        _run_phase("monitor", rows, handler)
        return

    round_num = 0
    results = []
    try:
        while True:
            round_num += 1
            print(f"\n=== monitor --watch: round {round_num} @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
            results = _run_phase("monitor", rows, handler, write_csv=False)
            if all(r["status"] == "verified" for r in results):
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        results_path = _write_results_csv("monitor", results)
        print(f"\nInterrupted. Results written to {results_path}")
        verified = sum(1 for r in results if r["status"] == "verified")
        print(f"stopped — {verified}/{len(rows)} rows verified")
        sys.exit(0)

    results_path = _write_results_csv("monitor", results)
    print(f"\nResults written to {results_path}")
    if args.rename_hostname:
        print(f"\nAll {len(rows)} row(s) verified.")
    else:
        print(
            f"\nAll {len(rows)} row(s) verified. Run "
            f"`python onboard_extended_nodes.py monitor --csv {args.csv} --rename-hostname` "
            "to push hostnames now."
        )


def build_arg_parser():
    # Attached only to each subparser, not the top-level parser: argparse
    # subparsers re-apply their own parent-argument defaults into the shared
    # namespace, which silently clobbers a value already set by the
    # top-level parser if these flags were also defined there. Requiring
    # them after the subcommand (verb first, then flags) avoids that
    # clobbering bug entirely and matches the more natural usage pattern.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--base-url",
        default=os.environ.get("CATC_BASE_URL"),
        help="Catalyst Center base URL (env: CATC_BASE_URL; will prompt if omitted)",
    )
    common.add_argument(
        "--username",
        default=os.environ.get("CATC_USERNAME"),
        help="Catalyst Center username (env: CATC_USERNAME; will prompt if omitted)",
    )
    common.add_argument(
        "--cc-version",
        default=os.environ.get("CATC_CC_VERSION", dnac_client.DEFAULT_CC_VERSION),
        help="Catalyst Center API version to target (env: CATC_CC_VERSION; "
        f"default: {dnac_client.DEFAULT_CC_VERSION}). Must match Settings > About on your controller.",
    )
    common.add_argument(
        "--no-verify-ssl",
        action="store_true",
        default=_env_bool("CATC_NO_VERIFY_SSL"),
        help="Disable TLS certificate verification (env: CATC_NO_VERIFY_SSL; self-signed lab controllers only)",
    )

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_prepare = subparsers.add_parser(
        "prepare",
        help="Create fabric port channels for every row",
        parents=[common],
    )
    p_prepare.add_argument("--csv", required=True, help="Path to extended_nodes.csv")
    p_prepare.add_argument("--dry-run", action="store_true", help="Resolve and validate only, no writes")
    p_prepare.set_defaults(func=cmd_prepare)

    p_monitor = subparsers.add_parser(
        "monitor", help="Report inventory / fabric-role state for every row", parents=[common]
    )
    p_monitor.add_argument("--csv", required=True, help="Path to extended_nodes.csv")
    p_monitor.add_argument(
        "--watch", action="store_true",
        help="Keep polling in rounds (sleeping --interval seconds between them) until every row "
        "reaches 'verified', instead of a single pass. Ctrl+C stops cleanly.",
    )
    p_monitor.add_argument(
        "--interval", type=int, default=20,
        help="Seconds to sleep between rounds in --watch mode (default: 20). Ignored without --watch.",
    )
    p_monitor.add_argument(
        "--debug",
        action="store_true",
        help="Dump raw inventory/fabric-role API responses per device to logs/debug_<timestamp>/",
    )
    p_monitor.add_argument(
        "--ise",
        action="store_true",
        help="Also check/remediate each device's ISE Network Device Group membership (requires --ise-ndg)",
    )
    p_monitor.add_argument(
        "--ise-base-url",
        default=os.environ.get("ISE_BASE_URL"),
        help="ISE base URL, e.g. https://10.1.1.2:9060 (env: ISE_BASE_URL; will prompt if omitted)",
    )
    p_monitor.add_argument(
        "--ise-username",
        default=os.environ.get("ISE_USERNAME"),
        help="ISE ERS username (env: ISE_USERNAME). Defaults to the same username/password used for Catalyst "
        "Center (no extra prompt); pass this to use a different ISE account, which prompts for its own password.",
    )
    p_monitor.add_argument(
        "--ise-ndg",
        default=os.environ.get("ISE_NDG"),
        help="Full ERS NDG path to enforce, e.g. 'Device Type#All Device Types#SDA-Extended-Node' "
        "(env: ISE_NDG). Required if --ise is set.",
    )
    p_monitor.add_argument(
        "--ise-no-verify-ssl",
        action="store_true",
        default=_env_bool("ISE_NO_VERIFY_SSL"),
        help="Disable TLS certificate verification against ISE (env: ISE_NO_VERIFY_SSL; self-signed lab ISE only)",
    )
    p_monitor.add_argument(
        "--ise-dry-run",
        action="store_true",
        help="Look up and report the intended ISE NDG change without writing it",
    )
    p_monitor.add_argument(
        "--rename-hostname",
        action="store_true",
        help="Push the CSV's extended_node_hostname to the device over SSH (saved to running-config)",
    )
    p_monitor.add_argument(
        "--rename-dry-run",
        action="store_true",
        help="Report the intended hostname change without pushing config",
    )
    p_monitor.add_argument(
        "--device-type",
        default=os.environ.get("DEVICE_TYPE", "cisco_ios"),
        help="Netmiko device_type for the SSH hostname push (env: DEVICE_TYPE; default: cisco_ios)",
    )
    p_monitor.add_argument(
        "--device-port",
        type=int,
        default=_env_int("DEVICE_PORT", 22),
        help="SSH port for the hostname push (env: DEVICE_PORT; default: 22)",
    )
    p_monitor.set_defaults(func=cmd_monitor)

    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if getattr(args, "ise", False) and not args.ise_ndg:
        print("ERROR: --ise-ndg is required when --ise is set", file=sys.stderr)
        sys.exit(1)

    try:
        rows = load_rows(args.csv)
    except CsvValidationError as exc:
        print(f"ERROR: CSV validation failed:\n{exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"ERROR: CSV file not found: {args.csv}", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(rows)} row(s) from {args.csv}")

    dnac, catc_username, catc_password = dnac_client.connect(
        base_url=args.base_url,
        username=args.username,
        version=args.cc_version,
        verify=not args.no_verify_ssl,
    )
    resolver = Resolver(dnac)

    ise_session = None
    if getattr(args, "ise", False):
        # Default to the same account used for Catalyst Center so the
        # operator isn't prompted twice; --ise-username opts into a
        # different account, which then prompts for its own password.
        if args.ise_username:
            ise_username, ise_password = args.ise_username, None
        else:
            ise_username, ise_password = catc_username, catc_password
        ise_session = ise_client.connect(
            base_url=args.ise_base_url,
            username=ise_username,
            password=ise_password,
            verify=not args.ise_no_verify_ssl,
        )

    if args.command == "monitor":
        cmd_monitor(
            dnac,
            resolver,
            rows,
            args,
            ise_session=ise_session,
            catc_username=catc_username,
            catc_password=catc_password,
        )
    else:
        args.func(dnac, resolver, rows, args)


if __name__ == "__main__":
    main()
