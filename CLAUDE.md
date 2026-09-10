# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CLI that automates SD-Access extended node onboarding on Cisco Catalyst Center. It creates the PAgP port channel on the fabric edge switch (`connectedDeviceType=EXTENDED_NODE`), then polls Catalyst Center until the device shows up with fabric role `Extended Node`. `monitor --watch` turns that into a self-polling loop (default 20s interval) that keeps re-checking every row — including ones stuck at `failed`/`warning`, no retry cap — until all reach `verified`, so an admin can start it once and walk away instead of re-invoking `monitor` by hand. Two optional side-effects run in the same monitor poll loop: fixing the device's ISE Network Device Group membership, and pushing the CSV's intended hostname to the device over SSH.

It does not build the fabric, assign IP pools, or configure DHCP — those are assumed prerequisites (see README.md).

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Create port channels for every CSV row (before racking hardware)
python onboard_extended_nodes.py prepare --csv extended_nodes.csv --dry-run
python onboard_extended_nodes.py prepare --csv extended_nodes.csv

# Poll onboarding progress — safe to re-run repeatedly, stateless
python onboard_extended_nodes.py monitor --csv extended_nodes.csv
python onboard_extended_nodes.py monitor --csv extended_nodes.csv --debug   # dumps raw API responses to logs/debug_<ts>/<serial>.json
python onboard_extended_nodes.py monitor --csv extended_nodes.csv --watch --interval 20   # poll on its own until all rows verified; Ctrl+C to stop cleanly

# Optional side-effects, both run inside monitor's poll loop:
python onboard_extended_nodes.py monitor --csv extended_nodes.csv --ise --ise-dry-run
python onboard_extended_nodes.py monitor --csv extended_nodes.csv --rename-hostname --rename-dry-run
```

There is no test suite. Global flags (`--base-url`, `--username`, `--cc-version`, `--no-verify-ssl`) go *after* the subcommand. `--csv` and every run-mode flag are deliberately not `.env`-configurable — only constant connection/target settings belong there (see `.env.example`).

## Architecture

`onboard_extended_nodes.py` is the CLI entrypoint (argparse, two subcommands: `prepare`, `monitor`) and owns per-run orchestration: loading the `.env`, prompting for credentials once at the top of the run, iterating CSV rows through `_run_phase`, writing the timestamped results CSV, printing the summary. Everything device/API-specific lives in `lib/`, one client per external system:

- `lib/dnac_client.py` — Catalyst Center session (`connect()`), interactive credential prompt, and `poll_task()`, the shared task-polling helper every writer (port channel creation) waits on.
- `lib/resolvers.py` — `Resolver`, instantiated once per run and reused across all rows. Walks site hierarchy → fabric site/zone → device IDs, caching each lookup internally so repeated rows sharing a site or fabric edge don't re-query. `resolve_fabric_id_for_hierarchy` falls back from fabric site to nearest ancestor fabric zone — extended nodes are frequently declared at a site that's a zone, not a site, in the fabric hierarchy.
- `lib/port_channels.py` — the `prepare` write path: checks existing port channels for idempotency (`skipped` if the target interfaces are already channel members), then creates the PAgP channel. `get_fabric_device_roles` is also what `monitor` polls to detect `Extended Node` role.
- `lib/csv_loader.py` — CSV → `ExtendedNodeRow` dataclasses, with validation and comment/blank-line skipping. This is the row shape threaded through both subcommands.
- `lib/ise_client.py` — separate ERS session against ISE (`monitor --ise` only). Looks up the device **by the live Catalyst Center-assigned hostname (`SN-<serial>`)**, not the CSV's hostname column, then remediates NDG membership within one category path only, idempotently.
- `lib/hostname_client.py` — Netmiko SSH session (`monitor --rename-hostname` only), pushes and saves the running-config hostname. No dnacentersdk intent API exists for this, hence the direct SSH.

Key cross-cutting behaviors, since they're easy to regress:

- **Credentials are never persisted.** Passwords are always `getpass`-prompted, never accepted as a CLI arg, `.env` value, or logged. ISE and SSH both default to reusing the already-prompted Catalyst Center credentials rather than prompting again — only diverge (`--ise-username`) if a different account is actually needed, and don't restructure the code so a password could flow through `.env` or a file.
- **`monitor` is stateless and idempotent by design.** Every check (fabric role, ISE NDG, hostname) re-derives current state from the live source of truth (Catalyst Center inventory / ISE / device running-config) rather than trusting the CSV or a prior run's output, and skips the write when already-compliant. Preserve this if extending monitor — don't introduce local state that could drift from the device. `--watch` relies on this: it re-runs the same per-row handler every round with no per-row state carried between rounds.
- **`--watch`'s results CSV is written once, not per round.** `_run_phase(..., write_csv=False)` is used for every round except effectively the last; `KeyboardInterrupt` is a `BaseException` so it isn't caught by `_run_phase`'s per-row `except Exception`, meaning it propagates out mid-round and the outer `results` variable still holds the last *fully completed* round — that's what gets written on Ctrl+C, not partial/in-flight results.
- **`status` vs `detail`.** The primary `status` column on a monitor row is driven only by Catalyst Center state (`not-seen`/`pending`/`warning`/`verified`). ISE and rename outcomes are informational suffixes appended to `detail`; they must never change `status` — a device can be `verified` with an `ISE: updated` or `rename: updated` note, but ISE/SSH failures don't fail the row.
- **One bad row never aborts the batch** — `_run_phase` isolates per-row exceptions into that row's result.
- **No Catalyst Center resync after hostname rename.** An on-demand `sync_devices_using_forcesync` call was tried and dropped (see git history) — it collided with Catalyst Center's own in-flight provisioning sync. Don't re-add it without re-validating against a real in-progress onboarding.
- **PnP pre-staging was removed intentionally** (see git history) — pre-claiming an extended node in PnP causes Catalyst Center to onboard it as an edge node instead of an extended node. Don't reintroduce a PnP claim step into `prepare`.
- **SDK version coupling**: `requirements.txt` pins `dnacentersdk==2.8.14` against Catalyst Center 2.3.7.9. Payload validation errors on a different controller version usually mean this pin and `--cc-version`/`CATC_CC_VERSION` are out of sync with the target controller, not a code bug.
