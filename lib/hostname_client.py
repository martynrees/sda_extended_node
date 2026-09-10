"""Netmiko-based device hostname push for extended nodes.

SSHes directly to the device (no dnacentersdk intent API exists for this),
sets the running-config hostname to match the CSV, and saves it. Reuses the
same Catalyst Center username/password already prompted for at the top of
main() — accounts are assumed to land directly in privileged EXEC (priv 15)
over SSH, so no enable/secret handling is done here.
"""

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException


class HostnameError(Exception):
    pass


def push_hostname(host, username, password, new_hostname, device_type="cisco_ios", port=22, dry_run=False):
    """Ensure the device's running-config hostname matches new_hostname.

    Returns {"status": "unchanged"|"dry-run"|"updated", "detail": str}.
    Raises HostnameError on any connection/auth/config failure.
    """
    try:
        conn = ConnectHandler(
            device_type=device_type,
            host=host,
            username=username,
            password=password,
            port=port,
        )
    except (NetmikoTimeoutException, NetmikoAuthenticationException) as exc:
        raise HostnameError(f"SSH connection to {host} failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surface any connection error clearly
        raise HostnameError(f"SSH connection to {host} failed: {exc}") from exc

    try:
        current_hostname = conn.find_prompt().strip().rstrip("#>")

        if current_hostname.lower() == new_hostname.lower():
            return {"status": "unchanged", "detail": f"hostname already '{current_hostname}'"}

        if dry_run:
            return {"status": "dry-run", "detail": f"would rename '{current_hostname}' -> '{new_hostname}'"}

        conn.send_config_set([f"hostname {new_hostname}"])
        # Netmiko cached the pre-rename prompt as base_prompt at connect time and
        # uses it (unrefreshed) to detect "am I in enable mode?" inside
        # save_config() -> enable() -> check_enable_mode(). Since the device's
        # prompt just changed, that stale pattern never matches and save_config()
        # raises before it ever sends the actual save command -- silently leaving
        # the rename applied only to the running config, not saved. Re-detecting
        # the prompt here (generically, not against the old hostname) fixes that.
        conn.set_base_prompt()
        conn.save_config()
        return {"status": "updated", "detail": f"renamed '{current_hostname}' -> '{new_hostname}'"}
    except (NetmikoTimeoutException, NetmikoAuthenticationException) as exc:
        raise HostnameError(f"hostname push to {host} failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surface any config-push error clearly
        raise HostnameError(f"hostname push to {host} failed: {exc}") from exc
    finally:
        conn.disconnect()
