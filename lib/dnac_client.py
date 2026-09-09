"""Catalyst Center connection handling.

Credentials are never written to disk and never accepted as CLI flags for the
password. Base URL and username may be passed on the command line for
convenience; the password is always prompted interactively via getpass.
"""

import getpass
import sys
import time

from dnacentersdk import DNACenterAPI
from dnacentersdk.exceptions import ApiError

DEFAULT_CC_VERSION = "2.3.7.9"

DEFAULT_TASK_TIMEOUT = 120
DEFAULT_TASK_POLL_INTERVAL = 3


class TaskError(Exception):
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
            raise TaskError(f"task {task_id} failed: {task.get('failureReason') or task.get('progress')}")

        if task.get("endTime"):
            return task

        time.sleep(interval)

    raise TaskError(f"task {task_id} did not complete within {timeout}s")


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
    """Prompt for credentials and return (dnac, username, password).

    The credentials are returned alongside the client so callers that also
    need to authenticate elsewhere (e.g. ISE) can reuse them instead of
    prompting a second time — still only ever held in memory, never written
    to disk or logged.

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
    return dnac, username, password
