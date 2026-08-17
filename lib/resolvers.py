"""Site / fabric / device resolution helpers, cached per run.

Every onboarding row needs the same three lookups (site -> fabric -> edge
device). These are cached by input value for the lifetime of a single CLI
invocation so a batch CSV with many rows pointing at the same site/edge
switch doesn't re-query Catalyst Center for each row.
"""

import re

IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


class ResolverError(Exception):
    pass


class Resolver:
    def __init__(self, dnac):
        self.dnac = dnac
        self._site_cache = {}
        self._fabric_cache = {}
        self._device_cache = {}

    def resolve_site_id(self, site_hierarchy: str) -> str:
        if site_hierarchy in self._site_cache:
            return self._site_cache[site_hierarchy]

        response = self.dnac.sites.get_site_v2(group_name_hierarchy=site_hierarchy)
        items = response.get("response") if isinstance(response, dict) else response.response
        if not items:
            raise ResolverError(f"no site found matching hierarchy '{site_hierarchy}'")
        if len(items) > 1:
            raise ResolverError(
                f"site hierarchy '{site_hierarchy}' matched {len(items)} sites, expected exactly 1"
            )

        site_id = items[0]["id"]
        self._site_cache[site_hierarchy] = site_id
        return site_id

    def resolve_fabric_id(self, site_id: str) -> str:
        if site_id in self._fabric_cache:
            return self._fabric_cache[site_id]

        response = self.dnac.sda.get_fabric_sites(site_id=site_id)
        items = response.get("response") if isinstance(response, dict) else response.response
        if not items:
            raise ResolverError(
                f"site_id '{site_id}' is not a fabric site (no fabric found) — "
                "confirm this site has already been added to a fabric in Catalyst Center"
            )

        # get_fabric_sites returns the fabric's own identifier as "id", not
        # "fabricId" (confirmed against a live response).
        fabric_id = items[0]["id"]
        self._fabric_cache[site_id] = fabric_id
        return fabric_id

    def resolve_device_id(self, identifier: str) -> str:
        if identifier in self._device_cache:
            return self._device_cache[identifier]

        if IP_RE.match(identifier):
            response = self.dnac.devices.get_device_list(management_ip_address=identifier)
        else:
            response = self.dnac.devices.get_device_list(hostname=identifier)

        items = response.get("response") if isinstance(response, dict) else response.response
        if not items:
            raise ResolverError(f"no network device found matching '{identifier}'")
        if len(items) > 1:
            raise ResolverError(f"'{identifier}' matched {len(items)} devices, expected exactly 1")

        device_id = items[0]["id"]
        self._device_cache[identifier] = device_id
        return device_id
