"""PnP (Plug and Play) pre-staging: import + claim before a device connects."""


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


def _import_device(dnac, row):
    payload = [
        {
            "deviceInfo": {
                "hostname": row.extended_node_hostname,
                "serialNumber": row.extended_node_serial,
                "pid": row.extended_node_pid,
            }
        }
    ]
    dnac.device_onboarding_pnp.import_devices_in_bulk(payload=payload)


def stage_device(dnac, resolver, row, dry_run=False):
    """Pre-stage a device in PnP and claim it to its site, before it connects.

    Imports the serial/PID if PnP doesn't know about it yet, then claims it
    to its site. Safe to run ahead of racking the device: once claimed, the
    device self-provisions the moment it boots, gets DHCP, and dials home —
    no further manual step is required. Also safe to re-run: a device that
    already dialed home (imported itself) gets claimed as-is, and one that
    is already Provisioned is skipped.
    """
    pnp_device = get_pnp_device(dnac, row.extended_node_serial)

    if pnp_device is None:
        if dry_run:
            return {"status": "dry-run", "detail": f"would import and claim {row.extended_node_serial}"}
        _import_device(dnac, row)
        pnp_device = get_pnp_device(dnac, row.extended_node_serial)
        if pnp_device is None:
            raise PnpError(f"serial '{row.extended_node_serial}' not found in PnP inventory after import")

    device_info = pnp_device.get("deviceInfo", {})
    if device_info.get("state") == "Provisioned":
        return {"status": "skipped", "detail": f"{row.extended_node_serial} already Provisioned"}

    device_id = pnp_device["id"]
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
    return {
        "status": "staged",
        "detail": f"claimed {row.extended_node_serial} to site {site_id} — will self-provision on dial-home",
    }
