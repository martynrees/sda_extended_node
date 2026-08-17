"""Catalyst Center connection handling.

Credentials are never written to disk and never accepted as CLI flags for the
password. Base URL and username may be passed on the command line for
convenience; the password is always prompted interactively via getpass.
"""

import getpass
import sys

from dnacentersdk import DNACenterAPI
from dnacentersdk.exceptions import ApiError

DEFAULT_CC_VERSION = "2.3.7.9"


def prompt_for_credentials(base_url=None, username=None):
    if not base_url:
        base_url = input("Catalyst Center base URL (e.g. https://10.1.1.1): ").strip()
    if not base_url.startswith("http"):
        base_url = "https://" + base_url

    if not username:
        username = input("Username: ").strip()

    password = getpass.getpass("Password: ")

    return base_url, username, password


def connect(base_url=None, username=None, version=DEFAULT_CC_VERSION, verify=True):
    """Prompt for credentials and return a connected DNACenterAPI client.

    `version` must match the controller's exact patch release (Settings >
    About in the Catalyst Center UI) — mismatches are a common source of
    subtle payload-shape errors that show up as confusing validation
    failures rather than a clear version error.
    """
    base_url, username, password = prompt_for_credentials(base_url, username)

    print(f"Connecting to {base_url} (API version {version})...")
    try:
        dnac = DNACenterAPI(
            username=username,
            password=password,
            base_url=base_url,
            version=version,
            verify=verify,
        )
        # Cheap authenticated call to fail fast on bad credentials/version.
        # get_site_v2's limit/offset are typed as str by the SDK (unlike
        # most other endpoints, which take native int) — passing a native
        # int here throws a confusing SDK-internal type error, confirmed
        # live against a real lab controller.
        dnac.sites.get_site_v2(limit="1")
    except ApiError as exc:
        print(f"ERROR: could not authenticate against Catalyst Center: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - surface any connection error clearly
        print(f"ERROR: connection failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Connected.")
    return dnac
