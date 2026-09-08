"""ISE ERS API client for Network Device Group (NDG) remediation.

Credentials follow the same UX as lib/dnac_client.py: base-url/username via
flag or prompt, password always getpass, never logged/written/CLI-arg.

CAVEAT: unlike this codebase's SDA quirks (each confirmed against a live
deployment), this ERS contract is written from documented ISE ERS API
behavior only, not yet validated against this customer's live ISE. Run
`monitor --ise --ise-dry-run` against one real device first and confirm the
reported before/after NDG values look right before trusting it unattended
across a batch.
"""

import getpass
import sys

import requests

DEFAULT_ERS_PORT = 9060


class IseError(Exception):
    pass


class IseSession:
    """Thin wrapper around a requests.Session + base_url so call sites don't
    repeat auth/verify/base_url plumbing."""

    def __init__(self, session, base_url):
        self.session = session
        self.base_url = base_url.rstrip("/")

    def get(self, path, **kwargs):
        return self.session.get(f"{self.base_url}{path}", **kwargs)

    def put(self, path, **kwargs):
        return self.session.put(f"{self.base_url}{path}", **kwargs)


def prompt_for_credentials(base_url=None, username=None):
    if not base_url:
        base_url = input(f"ISE base URL (e.g. https://10.1.1.2:{DEFAULT_ERS_PORT}): ").strip()
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    host_part = base_url.split("://", 1)[1]
    if ":" not in host_part:
        base_url = f"{base_url}:{DEFAULT_ERS_PORT}"

    if not username:
        username = input("ISE username: ").strip()

    password = getpass.getpass("ISE password: ")

    return base_url, username, password


def connect(base_url=None, username=None, verify=True):
    """Prompt for credentials and return a connected IseSession.

    Fails fast with a clear error on bad credentials/connectivity via a
    cheap authenticated call, same intent as dnac_client.connect().
    """
    base_url, username, password = prompt_for_credentials(base_url, username)

    print(f"Connecting to ISE {base_url}...")
    session = requests.Session()
    session.auth = (username, password)
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    session.verify = verify

    ise = IseSession(session, base_url)

    try:
        resp = ise.get("/ers/config/networkdevice", params={"size": 1}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"ERROR: could not authenticate against ISE: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Connected to ISE.")
    return ise


def get_network_device_by_hostname(session, hostname):
    """Look up an ISE network-device object by hostname.

    Returns the unwrapped NetworkDevice dict, or None if no device with that
    hostname exists yet (e.g. Catalyst Center's TACACS-triggered auto-create
    in ISE hasn't happened yet). Raises IseError if the hostname filter
    matches more than one device (ambiguous — should never happen for a
    unique hostname, but ISE data can be messy).
    """
    try:
        resp = session.get(
            "/ers/config/networkdevice",
            params={"filter": f"name.EQ.{hostname}"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise IseError(f"lookup failed for hostname {hostname!r}: {exc}") from exc

    resources = ((resp.json().get("SearchResult") or {}).get("resources")) or []

    if not resources:
        return None
    if len(resources) > 1:
        raise IseError(
            f"hostname {hostname!r} matched {len(resources)} ISE network devices "
            "(ambiguous) - resolve manually"
        )

    device_id = resources[0]["id"]

    try:
        resp = session.get(f"/ers/config/networkdevice/{device_id}", timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise IseError(f"fetch failed for ISE network device id {device_id}: {exc}") from exc

    return resp.json().get("NetworkDevice")


def ensure_ndg_membership(session, device_obj, ndg_value, dry_run=False):
    """Ensure device_obj is a member of the NDG identified by ndg_value.

    ndg_value is a full ERS NDG path, e.g.
    "Device Type#All Device Types#SDA-Extended-Node". Only the membership
    within the same category (the segment before the first '#') is replaced
    — other category memberships (Location, IPSEC, etc.) are left untouched.

    Idempotent: only PUTs when the NDG actually needs to change, so re-running
    this against a device already in the right group is a no-op.
    """
    category = ndg_value.split("#", 1)[0]
    ndg_list = list(device_obj.get("NetworkDeviceGroupList") or [])

    existing_index = None
    existing_value = None
    for i, entry in enumerate(ndg_list):
        if entry.split("#", 1)[0] == category:
            existing_index = i
            existing_value = entry
            break

    if existing_value == ndg_value:
        return {"status": "unchanged", "detail": f"already in '{ndg_value}'"}

    if existing_index is not None:
        detail = f"'{existing_value}' -> '{ndg_value}'"
    else:
        detail = f"add '{ndg_value}' (no existing '{category}' membership)"

    if dry_run:
        return {"status": "would-update", "detail": detail}

    if existing_index is not None:
        ndg_list[existing_index] = ndg_value
    else:
        ndg_list.append(ndg_value)

    updated_device = dict(device_obj)
    updated_device["NetworkDeviceGroupList"] = ndg_list
    device_id = updated_device.get("id")

    try:
        resp = session.put(
            f"/ers/config/networkdevice/{device_id}",
            json={"NetworkDevice": updated_device},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise IseError(f"update failed for ISE network device id {device_id}: {exc}") from exc

    return {"status": "updated", "detail": detail}
