"""Port-channel creation for extended-node fabric edge uplinks.

Hardcodes connectedDeviceType=EXTENDED_NODE and protocol=PAGP — PAgP is the
only protocol Catalyst Center accepts for this connectedDeviceType, so it is
not a customer-configurable option.
"""

import time

CONNECTED_DEVICE_TYPE = "EXTENDED_NODE"
PROTOCOL = "PAGP"

DEFAULT_TASK_TIMEOUT = 120
DEFAULT_TASK_POLL_INTERVAL = 3


class PortChannelError(Exception):
    pass


def _as_dict(obj):
    """dnacentersdk responses are MyDict objects; normalise to plain dict access."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


def poll_task(dnac, task_id, timeout=DEFAULT_TASK_TIMEOUT, interval=DEFAULT_TASK_POLL_INTERVAL):
    """Poll a Catalyst Center task until it completes, errors, or times out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = dnac.task.get_task_by_id(task_id=task_id)
        task = _as_dict(result.response if hasattr(result, "response") else result.get("response"))

        if task.get("isError"):
            raise PortChannelError(f"task {task_id} failed: {task.get('failureReason') or task.get('progress')}")

        if task.get("endTime"):
            return task

        time.sleep(interval)

    raise PortChannelError(f"task {task_id} did not complete within {timeout}s")


def get_existing_port_channels(dnac, fabric_id, network_device_id):
    response = dnac.sda.get_port_channels(fabric_id=fabric_id, network_device_id=network_device_id)
    items = response.get("response") if isinstance(response, dict) else response.response
    return items or []


def _interfaces_already_in_use(existing_channels, requested_interfaces):
    requested = set(requested_interfaces)
    in_use = set()
    for channel in existing_channels:
        channel_interfaces = set(channel.get("interfaceNames") or [])
        in_use |= channel_interfaces & requested
    return in_use


def get_fabric_device_roles(dnac, management_ip_address):
    """Return the SDA fabric role(s) of a device, e.g. ["Extended Node"].

    Uses sda.get_device_role_in_sda_fabric(), confirmed against a live
    Catalyst Center deployment. Two other approaches were tried and ruled
    out first:

    - devices.get_device_list()'s generic `role` field (ACCESS/DISTRIBUTION/
      CORE/BORDER ROUTER) is a CDP-topology classification unrelated to
      fabric role; it returned "ACCESS" for a confirmed live extended node.
    - sda.get_fabric_devices(fabric_id, network_device_id, device_roles=...)
      documents EDGE_NODE/EXTENDED_NODE as allowed deviceRoles filter
      values, but in practice only returns BORDER_NODE/CONTROL_PLANE_NODE/
      WIRELESS_CONTROLLER_NODE devices on this Catalyst Center version —
      it never surfaced the same known-good extended node.

    get_device_role_in_sda_fabric() takes only a management IP (no fabric_id
    needed) and returns {"roles": [...], "status": ..., "description": ...}.
    Note the value is the human-readable string "Extended Node", not the
    "EXTENDED_NODE" enum constant used elsewhere in the SDA API surface.
    """
    response = dnac.sda.get_device_role_in_sda_fabric(device_management_ip_address=management_ip_address)
    response_dict = _as_dict(response)
    return response_dict.get("roles") or []


def create_port_channel(dnac, resolver, row, dry_run=False):
    """Create the PAgP port channel for one CSV row.

    Returns a dict describing the outcome: {"status": "created"|"skipped", "detail": str}
    Raises PortChannelError on unrecoverable failure for this row.
    """
    site_id = resolver.resolve_site_id(row.site_hierarchy)
    fabric_id = resolver.resolve_fabric_id(site_id)
    device_id = resolver.resolve_device_id(row.fabric_edge_identifier)

    existing = get_existing_port_channels(dnac, fabric_id, device_id)
    clashing = _interfaces_already_in_use(existing, row.edge_port_channel_interfaces)
    if clashing:
        return {
            "status": "skipped",
            "detail": (
                f"interface(s) already part of an existing port channel on "
                f"{row.fabric_edge_identifier}: {', '.join(sorted(clashing))}"
            ),
        }

    payload = [
        {
            "fabricId": fabric_id,
            "networkDeviceId": device_id,
            "interfaceNames": row.edge_port_channel_interfaces,
            "connectedDeviceType": CONNECTED_DEVICE_TYPE,
            "protocol": PROTOCOL,
            "description": row.port_channel_description
            or f"Extended node uplink: {row.extended_node_hostname}",
        }
    ]

    if dry_run:
        return {
            "status": "dry-run",
            "detail": f"would create port channel on {row.fabric_edge_identifier} "
            f"({device_id}) fabric {fabric_id} interfaces={row.edge_port_channel_interfaces}",
        }

    response = dnac.sda.add_port_channels(payload=payload)
    response_dict = _as_dict(response)
    task_id = (response_dict.get("response") or {}).get("taskId") or response_dict.get("taskId")
    if not task_id:
        raise PortChannelError(f"add_port_channels did not return a taskId: {response_dict}")

    task = poll_task(dnac, task_id)
    return {"status": "created", "detail": f"task {task_id} completed: {task.get('progress')}"}
