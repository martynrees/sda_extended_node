"""PnP (Plug and Play) status lookup, optional pre-stage import, and site claim."""

import time

DEFAULT_CLAIM_POLL_TIMEOUT = 300
DEFAULT_CLAIM_POLL_INTERVAL = 10

TERMINAL_SUCCESS_STATES = {"Provisioned"}
TERMINAL_ERROR_STATES = {"Error", "Failed"}


class PnpError(Exception):
    pass


def _as_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


def get_pnp_device(dnac, serial_number):
    """Return the PnP device record for a serial number, or None if not seen yet."""
    response = dnac.device_onboarding_pnp.get_device_list(serial_number=serial_number)
    items = response if isinstance(response, list) else _as_dict(response)
    if isinstance(items, dict):
        items = items.get("response") or []
    if not items:
        return None
    return _as_dict(items[0])


def describe_status(pnp_device):
    if pnp_device is None:
        return "not seen (device has not dialed home to Catalyst Center yet)"
    device_info = pnp_device.get("deviceInfo", {})
    state = device_info.get("state", "unknown")
    onb_state = (pnp_device.get("systemResetWorkflow") or {}).get("state", "")
    detail = f"state={state}"
    if onb_state:
        detail += f", onboarding={onb_state}"
    return detail


def import_device(dnac, row, dry_run=False):
    """Pre-stage a device in PnP inventory before it has dialed home.

    Used as a fallback when `claim --import-if-missing` is passed and the
    serial isn't found via DHCP-triggered auto-registration.
    """
    payload = [
        {
            "deviceInfo": {
                "hostname": row.extended_node_hostname,
                "serialNumber": row.extended_node_serial,
                "pid": row.extended_node_pid,
            }
        }
    ]
    if dry_run:
        return {"status": "dry-run", "detail": f"would import PnP device {row.extended_node_serial}"}

    dnac.device_onboarding_pnp.import_devices_in_bulk(payload=payload)
    return {"status": "imported", "detail": f"pre-staged PnP device {row.extended_node_serial}"}


def claim_device(dnac, resolver, row, dry_run=False, import_if_missing=False):
    """Claim a device to its site. Returns a dict with status/detail.

    Raises PnpError if the device cannot be found (and import wasn't
    requested/didn't help) or the claim call itself fails.
    """
    pnp_device = get_pnp_device(dnac, row.extended_node_serial)

    if pnp_device is None:
        if not import_if_missing:
            raise PnpError(
                f"serial '{row.extended_node_serial}' not found in PnP inventory yet — "
                "run 'status' first to confirm the device has dialed home, or pass "
                "--import-if-missing to pre-stage it"
            )
        import_device(dnac, row, dry_run=dry_run)
        if dry_run:
            return {
                "status": "dry-run",
                "detail": f"would import then claim {row.extended_node_serial}",
            }
        pnp_device = get_pnp_device(dnac, row.extended_node_serial)
        if pnp_device is None:
            raise PnpError(
                f"serial '{row.extended_node_serial}' still not found in PnP inventory after import"
            )

    device_id = pnp_device["id"]
    device_info = pnp_device.get("deviceInfo", {})
    current_state = device_info.get("state")
    if current_state == "Provisioned":
        return {"status": "skipped", "detail": f"{row.extended_node_serial} already Provisioned"}

    site_id = resolver.resolve_site_id(row.site_hierarchy)

    claim_payload = {
        "deviceId": device_id,
        "siteId": site_id,
        "type": "Default",
        "hostname": row.extended_node_hostname,
    }
    if row.image_id:
        claim_payload["imageInfo"] = {"imageId": row.image_id}
    if row.config_id:
        claim_payload["configInfo"] = {"configId": row.config_id}

    if dry_run:
        return {
            "status": "dry-run",
            "detail": f"would claim {row.extended_node_serial} (device_id={device_id}) to site {site_id}",
        }

    dnac.device_onboarding_pnp.claim_a_device_to_a_site(payload=claim_payload)
    final_state = poll_claim_state(dnac, row.extended_node_serial)
    return {"status": "claimed", "detail": f"final PnP state: {final_state}"}


def poll_claim_state(
    dnac,
    serial_number,
    timeout=DEFAULT_CLAIM_POLL_TIMEOUT,
    interval=DEFAULT_CLAIM_POLL_INTERVAL,
):
    """Poll PnP state after a claim until Provisioned, an error state, or timeout."""
    deadline = time.time() + timeout
    last_state = "unknown"
    while time.time() < deadline:
        pnp_device = get_pnp_device(dnac, serial_number)
        if pnp_device is not None:
            last_state = pnp_device.get("deviceInfo", {}).get("state", last_state)
            if last_state in TERMINAL_SUCCESS_STATES:
                return last_state
            if last_state in TERMINAL_ERROR_STATES:
                raise PnpError(f"{serial_number} entered error state '{last_state}' during claim")
        time.sleep(interval)

    raise PnpError(
        f"{serial_number} did not reach 'Provisioned' within {timeout}s "
        f"(last observed state: '{last_state}') — check 'status' again once the "
        "device has finished booting and rerun 'verify'"
    )
