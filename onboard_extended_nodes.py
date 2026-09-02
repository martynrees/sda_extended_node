#!/usr/bin/env python3
"""Extended-node onboarding automation for Catalyst Center (SD-Access fabric).

    python onboard_extended_nodes.py prepare --csv extended_nodes.csv [--dry-run]
    # ... rack, cable, power on the extended node(s) whenever ready ...
    python onboard_extended_nodes.py monitor --csv extended_nodes.csv

See README.md for full usage and manual prerequisites.
"""

import argparse
import csv as csv_module
import json
import os
import sys
import time
import traceback

from lib import dnac_client, port_channels
from lib.csv_loader import CsvValidationError, load_rows
from lib.resolvers import Resolver, ResolverError

LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


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


def _run_phase(phase_name, rows, row_handler):
    """Run row_handler(row) for every row, catching per-row errors.

    row_handler must return {"status": ..., "detail": ...}. Any exception is
    caught and recorded as status="failed" so one bad row cannot abort the
    batch.
    """
    results = []
    for row in rows:
        try:
            outcome = row_handler(row)
        except (ResolverError, port_channels.PortChannelError) as exc:
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


def cmd_monitor(dnac, resolver, rows, args):
    """Report each device's current discovery / fabric-role state.

    Stateless and safe to re-run at any time, in any order — devices that
    haven't connected yet just report "not-seen"; there's no requirement to
    wait for the whole batch before checking on the ones that are ready.
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
        role_response = port_channels.get_device_role_response(dnac, management_ip)
        debug_payload["fabric_role_response"] = role_response
        fabric_roles = role_response.get("roles") or []

        if debug_dir:
            path = _dump_debug(debug_dir, row.extended_node_serial, debug_payload)
            print(f"    debug dump: {path}")

        if "Extended Node" not in fabric_roles:
            return {
                "status": "warning",
                "detail": f"reachability={reachability}, fabric roles={fabric_roles or 'none'} "
                "-- visible in inventory but fabric role is not 'Extended Node', check manually",
            }

        return {"status": "verified", "detail": f"reachability={reachability}, fabric roles={fabric_roles}"}

    _run_phase("monitor", rows, handler)


def build_arg_parser():
    # Attached only to each subparser, not the top-level parser: argparse
    # subparsers re-apply their own parent-argument defaults into the shared
    # namespace, which silently clobbers a value already set by the
    # top-level parser if these flags were also defined there. Requiring
    # them after the subcommand (verb first, then flags) avoids that
    # clobbering bug entirely and matches the more natural usage pattern.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", help="Catalyst Center base URL (will prompt if omitted)")
    common.add_argument("--username", help="Catalyst Center username (will prompt if omitted)")
    common.add_argument(
        "--cc-version",
        default=dnac_client.DEFAULT_CC_VERSION,
        help=f"Catalyst Center API version to target (default: {dnac_client.DEFAULT_CC_VERSION}). "
        "Must match Settings > About on your controller.",
    )
    common.add_argument(
        "--no-verify-ssl",
        action="store_true",
        help="Disable TLS certificate verification (self-signed lab controllers only)",
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
        "--debug",
        action="store_true",
        help="Dump raw inventory/fabric-role API responses per device to logs/debug_<timestamp>/",
    )
    p_monitor.set_defaults(func=cmd_monitor)

    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        rows = load_rows(args.csv)
    except CsvValidationError as exc:
        print(f"ERROR: CSV validation failed:\n{exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"ERROR: CSV file not found: {args.csv}", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(rows)} row(s) from {args.csv}")

    dnac = dnac_client.connect(
        base_url=args.base_url,
        username=args.username,
        version=args.cc_version,
        verify=not args.no_verify_ssl,
    )
    resolver = Resolver(dnac)

    args.func(dnac, resolver, rows, args)


if __name__ == "__main__":
    main()
