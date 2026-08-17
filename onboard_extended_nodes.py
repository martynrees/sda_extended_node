#!/usr/bin/env python3
"""Extended-node onboarding automation for Catalyst Center (SD-Access fabric).

Run phases in order, with the physical rack/cable/power step happening
between `portchannels` and `status`/`claim`:

    python onboard_extended_nodes.py portchannels --csv extended_nodes.csv [--dry-run]
    # ... rack, cable, power on the extended node(s) ...
    python onboard_extended_nodes.py status       --csv extended_nodes.csv
    python onboard_extended_nodes.py claim        --csv extended_nodes.csv [--dry-run] [--import-if-missing]
    python onboard_extended_nodes.py verify       --csv extended_nodes.csv

See README.md for the manual prerequisites this tool assumes are already in
place (IP pools, DHCP option 43/82, SNMP).
"""

import argparse
import csv as csv_module
import os
import sys
import time
import traceback

from lib import dnac_client, pnp, port_channels
from lib.csv_loader import CsvValidationError, load_rows
from lib.resolvers import Resolver, ResolverError

LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


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
        except (ResolverError, port_channels.PortChannelError, pnp.PnpError) as exc:
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


def cmd_portchannels(dnac, resolver, rows, args):
    def handler(row):
        return port_channels.create_port_channel(dnac, resolver, row, dry_run=args.dry_run)

    _run_phase("portchannels", rows, handler)


def cmd_status(dnac, resolver, rows, args):
    def handler(row):
        device = pnp.get_pnp_device(dnac, row.extended_node_serial)
        return {"status": "info", "detail": pnp.describe_status(device)}

    _run_phase("status", rows, handler)


def cmd_claim(dnac, resolver, rows, args):
    def handler(row):
        return pnp.claim_device(
            dnac, resolver, row, dry_run=args.dry_run, import_if_missing=args.import_if_missing
        )

    _run_phase("claim", rows, handler)


def cmd_verify(dnac, resolver, rows, args):
    def handler(row):
        pnp_device = pnp.get_pnp_device(dnac, row.extended_node_serial)
        if pnp_device is None:
            return {"status": "failed", "detail": "device not found in PnP inventory"}

        pnp_state = pnp_device.get("deviceInfo", {}).get("state")
        if pnp_state != "Provisioned":
            return {"status": "failed", "detail": f"PnP state is '{pnp_state}', expected 'Provisioned'"}

        response = dnac.devices.get_device_list(serial_number=row.extended_node_serial)
        items = response.get("response") if isinstance(response, dict) else response.response
        if not items:
            return {
                "status": "warning",
                "detail": "Provisioned in PnP but not yet visible in device inventory "
                "(may still be dialing home/config-pushing — re-run verify shortly)",
            }

        device = items[0]
        reachability = device.get("reachabilityStatus", "unknown")
        management_ip = device.get("managementIpAddress")

        fabric_roles = port_channels.get_fabric_device_roles(dnac, management_ip)
        detail = f"reachability={reachability}, fabric roles={fabric_roles or 'none'}"

        if "Extended Node" not in fabric_roles:
            detail += (
                " -- WARNING: device is not registered with fabric role 'Extended Node'. "
                "This may mean the no-separate-provisioning-call assumption for this "
                "Catalyst Center version doesn't hold; see README 'Known assumption to validate'."
            )
            return {"status": "warning", "detail": detail}

        return {"status": "verified", "detail": detail}

    _run_phase("verify", rows, handler)


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

    p_portchannels = subparsers.add_parser(
        "portchannels", help="Create PAgP port channels on fabric edge switches", parents=[common]
    )
    p_portchannels.add_argument("--csv", required=True, help="Path to extended_nodes.csv")
    p_portchannels.add_argument("--dry-run", action="store_true", help="Resolve and validate only, no writes")
    p_portchannels.set_defaults(func=cmd_portchannels)

    p_status = subparsers.add_parser(
        "status", help="Check PnP dial-home status for each serial", parents=[common]
    )
    p_status.add_argument("--csv", required=True, help="Path to extended_nodes.csv")
    p_status.set_defaults(func=cmd_status)

    p_claim = subparsers.add_parser("claim", help="Claim each device to its site in PnP", parents=[common])
    p_claim.add_argument("--csv", required=True, help="Path to extended_nodes.csv")
    p_claim.add_argument("--dry-run", action="store_true", help="Resolve and validate only, no writes")
    p_claim.add_argument(
        "--import-if-missing",
        action="store_true",
        help="Pre-stage a device via bulk import if its serial isn't in PnP inventory yet",
    )
    p_claim.set_defaults(func=cmd_claim)

    p_verify = subparsers.add_parser(
        "verify", help="Post-claim spot check of provisioning state", parents=[common]
    )
    p_verify.add_argument("--csv", required=True, help="Path to extended_nodes.csv")
    p_verify.set_defaults(func=cmd_verify)

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
