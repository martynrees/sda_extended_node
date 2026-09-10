# Extended Node Onboarding Automation

Automates SD-Access extended node onboarding on Cisco Catalyst Center:

1. Creates the PAgP port channel on the fabric edge switch (`connectedDeviceType=EXTENDED_NODE`).
2. Optionally remediates the device's ISE Network Device Group membership (`monitor --ise`).
3. Optionally pushes the CSV's intended hostname to the device over SSH (`monitor --rename-hostname`).

Once the port channel exists, the extended node is discovered and onboarded natively over LLDP/CDP the moment it's racked, cabled into that port channel, and powered on — no PnP claim step. (An earlier version of this script also pre-staged the device in PnP before racking; that step has been removed because pre-claiming an extended node in PnP causes Catalyst Center to onboard it as an edge node instead.)

It does **not** build the fabric, assign IP pools, or configure DHCP — see [Prerequisites](#prerequisites).

## Install

```bash
git clone <this-repo>
cd extended-node-onboarding
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requires Python 3.8+. `requirements.txt` pins `dnacentersdk==2.8.14` (matches Catalyst Center 2.3.7.9). If your controller is a different patch release, check the [SDK compatibility matrix](https://developer.cisco.com/docs/dnac/#!getting-started/sdk-compatibility) and update both the pin and `--cc-version` — mismatches here surface as confusing payload validation errors, not a clear version error. Confirm your controller's exact version under **Settings > About**.

`monitor --rename-hostname` additionally requires `netmiko` (pinned `>=4.3,<5`), used to SSH directly to the extended node — there is no dnacentersdk intent API for setting a device's running-config hostname.

## Configuration (`.env`)

Copy `.env.example` to `.env` and fill in the values that are constant for this customer/lab (controller URL, ISE URL, target NDG, SSH device type, etc.) so they don't need to be typed on every command line:

```bash
cp .env.example .env
vi .env
```

`.env` is loaded automatically from the script's own directory (not the current working directory) if present — nothing to source or export. It's already in `.gitignore`, so a filled-in copy never gets committed. **CLI flags always override `.env` values**, so `.env` only removes repetition — it doesn't remove the ability to point a one-off run somewhere else.

**Passwords are never read from `.env`** (or any file) — they're always prompted interactively via a masked `getpass` prompt, same as always. Only non-secret connection/target settings belong in `.env`.

| Variable | Equivalent flag | Notes |
|---|---|---|
| `CATC_BASE_URL` | `--base-url` | Catalyst Center base URL |
| `CATC_USERNAME` | `--username` | Catalyst Center username |
| `CATC_CC_VERSION` | `--cc-version` | Must match Settings > About on your controller |
| `CATC_NO_VERIFY_SSL` | `--no-verify-ssl` | `true`/`false` (also accepts `yes`/`no`/`1`/`0`); self-signed lab controllers only |
| `ISE_BASE_URL` | `--ise-base-url` | Only used with `monitor --ise` |
| `ISE_USERNAME` | `--ise-username` | Optional — omit to reuse the Catalyst Center account |
| `ISE_NDG` | `--ise-ndg` | Confirm the real target NDG in the ISE GUI first — see the `--ise` section below |
| `ISE_NO_VERIFY_SSL` | `--ise-no-verify-ssl` | Self-signed lab ISE only |
| `DEVICE_TYPE` | `--device-type` | Netmiko driver for `monitor --rename-hostname`'s SSH push (default `cisco_ios`) |
| `DEVICE_PORT` | `--device-port` | SSH port for the hostname push (default `22`) |

`--csv` and the run-mode flags (`--dry-run`, `--watch`, `--interval`, `--debug`, `--ise`, `--ise-dry-run`, `--rename-hostname`, `--rename-dry-run`) are deliberately **not** `.env`-configurable — they vary per invocation (which batch, which mode) rather than being constant for the environment, and defaulting something like `--rename-hostname` or a dry-run flag to "on" silently from a file would be an easy way to surprise yourself.

## Usage

```bash
# 1. Fill in your CSV
cp templates/extended_nodes_template.csv extended_nodes.csv

# 2. Create port channels for every row (before racking hardware)
python onboard_extended_nodes.py prepare --csv extended_nodes.csv --dry-run   # check first
python onboard_extended_nodes.py prepare --csv extended_nodes.csv

# 3. Rack, cable, power on the extended nodes — any order, any time, no CLI needed

# 4. Check progress — re-run any time, on any subset that's ready
python onboard_extended_nodes.py monitor --csv extended_nodes.csv

# ...or let it poll on its own until every row is verified, instead of re-running by hand
python onboard_extended_nodes.py monitor --csv extended_nodes.csv --watch

# 5. Once a device is visible, push its intended hostname
python onboard_extended_nodes.py monitor --csv extended_nodes.csv --rename-hostname
```

`monitor` is stateless: run it as many times as you like while a batch comes online. Devices that haven't connected yet just report `not-seen`; devices that have already reached `verified` don't need re-checking.

Add `--watch` to have `monitor` poll on its own in rounds (default every 20s, override with `--interval`) instead of a single pass — useful for starting a run, then racking/cabling/powering on hardware without babysitting the terminal. It keeps polling every row, including any stuck at `failed` or `warning`, forever until all reach `verified`, then exits and prints the suggested next command (`--rename-hostname`). Ctrl+C at any point stops cleanly: it writes a results CSV from the last completed round, prints `stopped — X/N rows verified`, and exits 0 — no traceback. Only the final round's results CSV is written (not one per round) to avoid spamming `logs/`.

Add `--debug` to `monitor` to dump the raw inventory / fabric-role API responses per device to `logs/debug_<timestamp>/<serial>.json` — useful when validating this flow on a controller/version combination it hasn't been tested against yet, or if a device sits at `warning` and you need to see exactly what Catalyst Center returned.

### ISE Network Device Group remediation (`monitor --ise`)

Catalyst Center pushes TACACS config to the extended node as part of provisioning, which auto-creates the device as a network-device object in ISE at that point too. If that ISE object isn't in the Network Device Group (NDG) your ISE authorization policy expects, ISE denies the TACACS logon — locking Catalyst Center (and anyone else) out of the device the moment provisioning completes. This is specific to certain ISE policy setups, not a Catalyst Center bug, but `monitor --ise` can detect and fix it in the same poll loop you're already running during onboarding.

**Confirm your actual target NDG in the ISE GUI before running this against production** (Administration > Network Resources > Network Device Groups). The example below (`SDA-Extended-Node`) is illustrative only — do not run it verbatim. Pointing `--ise-ndg` at the wrong group moves compliant devices *out* of the NDG their authorization policy expects, i.e. it causes the exact TACACS lockout this feature exists to fix.

```bash
python onboard_extended_nodes.py monitor --csv extended_nodes.csv \
  --ise --ise-base-url <your-ise-hostname> \
  --ise-ndg "Device Type#All Device Types#<your-target-NDG>" --ise-dry-run   # check first

python onboard_extended_nodes.py monitor --csv extended_nodes.csv \
  --ise --ise-base-url <your-ise-hostname> \
  --ise-ndg "Device Type#All Device Types#<your-target-NDG>"
```

With `ISE_BASE_URL` and `ISE_NDG` set in `.env` (see [Configuration](#configuration-env) above), both reduce to just `--ise` / `--ise --ise-dry-run`.

- `--ise` enables the check; requires `--ise-ndg`.
- `--ise-ndg` is a single, fixed target NDG applied to every row in the batch — the full ERS path in `Category#Root#Leaf` form, e.g. `Device Type#All Device Types#SDA-Extended-Node`. Only the membership within that category is replaced; other category memberships (Location, IPSEC, etc.) are left untouched.
- `--ise-base-url` follows the same optional-flag-or-prompt pattern as Catalyst Center. `--ise-username`/password default to the **same credentials already used for Catalyst Center** — no second prompt. Pass `--ise-username` to use a different ISE account instead, which then prompts for its own password.
- `--ise-no-verify-ssl` disables TLS verification against ISE (self-signed lab ISE only).
- `--ise-dry-run` looks up the device and reports the NDG change it would make, without writing anything.
- The device is matched in ISE **by the live hostname from Catalyst Center's device inventory**, not the CSV's `extended_node_hostname` column — confirmed in testing that Catalyst Center names the device `SN-<serial>` in its own inventory (and therefore in what it auto-creates in ISE), consistently, not just transiently.
- The check runs as soon as the device is visible in Catalyst Center inventory, not gated on reaching `verified` — TACACS config can land before the fabric-role query settles. The ISE outcome is appended to the row's `detail` column as an informational suffix (e.g. `ISE: updated - '...' -> '...'`); it never changes the primary `status` column, which stays driven by Catalyst Center state only.
- Idempotent: an NDG change is only written when the device isn't already in the target NDG, so re-running `monitor --ise` repeatedly during onboarding won't churn ISE on every poll.

ISE lookup, NDG category-matching, and the "already compliant" (`unchanged`) path have been confirmed live. The actual write (`updated`) path — the PUT that corrects a genuinely wrong NDG — has not yet been exercised against a real misassigned device; treat that path as unconfirmed until it has been.

### Hostname rename (`monitor --rename-hostname`)

Catalyst Center auto-names a newly onboarded extended node `SN-<serial>` in its inventory. The CSV's `extended_node_hostname` column is otherwise tracking-only — nothing pushes it to the device. `--rename-hostname` closes that gap: it SSHes directly to the device (there's no dnacentersdk intent API for this), sets the running-config hostname to match the CSV, and saves it.

```bash
python onboard_extended_nodes.py monitor --csv extended_nodes.csv --rename-hostname --rename-dry-run   # check first
python onboard_extended_nodes.py monitor --csv extended_nodes.csv --rename-hostname
```

- `--rename-hostname` enables the check/push; requires no other flag to be set.
- `--rename-dry-run` reports the hostname change that would be made (or confirms it's already correct) without pushing config.
- `--device-type` (default `cisco_ios`) is the Netmiko driver used for the SSH session — override for extended-node hardware that isn't classic IOS.
- `--device-port` (default `22`) is the SSH port.
- SSH login reuses the **same Catalyst Center username/password** already prompted for at the top of the run — no separate device-credential flags. The account is assumed to land directly in privileged EXEC (priv 15); there is no `enable`/secret handling.
- The check runs as soon as the device is visible in Catalyst Center inventory, same gating as `--ise` — not gated on reaching `verified`.
- Idempotent: the live hostname (from Catalyst Center's inventory, not the CSV) is compared case-insensitively to the CSV's `extended_node_hostname` before pushing anything; a device that's already correctly named reports `rename: unchanged` and is left alone.
- The outcome is appended to the row's `detail` column as an informational suffix (e.g. `rename: updated - renamed 'SN-ABC123' -> 'closet-b-en1'`); it never changes the primary `status` column.
- This does **not** trigger a Catalyst Center resync. An on-demand `sync_devices_using_forcesync` call after the rename was tried and dropped — it conflicted with Catalyst Center's own in-flight provisioning/sync of the device during onboarding. Catalyst Center picks up the renamed device on its own; if you need it to be reflected immediately rather than on the next poll interval, trigger a resync manually from the Catalyst Center inventory UI for that device.

Credentials are always prompted interactively (`--base-url`/`--username` optional as flags or `.env` values, password always via masked `getpass` prompt — never a CLI arg, never an `.env`/file value, never logged, never written to disk).

Global flags (`--base-url`, `--username`, `--cc-version`, `--no-verify-ssl`) go **after** the subcommand, e.g. `prepare --csv extended_nodes.csv --no-verify-ssl`.

### `prepare` status values
| Status | Meaning |
|---|---|
| `ok` | Port channel created |
| `skipped` | Port channel already existed (interfaces already in a port channel) |
| `dry-run` | `--dry-run` was passed, nothing written |
| `failed` | See `detail` for the resolver/API error |

### `monitor` status values
| Status | Meaning |
|---|---|
| `not-seen` | Device not yet visible in Catalyst Center inventory |
| `pending` | Visible in inventory, but not yet provisioned/assigned to a site — still onboarding, re-run shortly |
| `warning` | Provisioned, but fabric role doesn't show `Extended Node` — check manually |
| `verified` | Visible in inventory and confirmed with fabric role `Extended Node` |

Every run writes a timestamped results CSV to `logs/` and prints a summary count at the end. One bad row never aborts the batch.

## CSV format

| Column | Required | Notes |
|---|---|---|
| `extended_node_hostname` | yes | Friendly name assigned during claim |
| `extended_node_serial` | yes | Device serial number |
| `extended_node_pid` | yes | Product ID, e.g. `IE-3400H-24T` |
| `site_hierarchy` | yes | Full site path, e.g. `Global/Site1/BuildingA/Level2` |
| `fabric_edge_identifier` | yes | Hostname or management IP of the fabric edge switch |
| `edge_port_channel_interfaces` | yes | `;`-separated, e.g. `GigabitEthernet1/0/47;GigabitEthernet1/0/48` (max 8) |
| `port_channel_description` | no | Defaults to `Extended node uplink: <hostname>` |
| `image_id` | no | Unused — kept for compatibility with older CSVs, safe to leave blank |
| `config_id` | no | Unused — kept for compatibility with older CSVs, safe to leave blank |
| `notes` | no | Ignored by the script — your own tracking column |

`connectedDeviceType` (`EXTENDED_NODE`) and `protocol` (`PAGP`) are hardcoded, not CSV columns — PAgP is the only protocol Catalyst Center accepts for this connectedDeviceType.

Lines starting with `#` in the first column, and blank lines, are ignored.

## Prerequisites

Assumed already in place — `prepare` will not fail loudly if these are missing, but the device will never reach `verified`:

- Fabric is built and the target site is already a fabric site
- IP pool assigned to `INFRA_VN` at the target site
- DHCP configured with IP-helper to Catalyst Center, option 43/82 intact
- SNMP configured at the site

## Repo layout

```
extended-node-onboarding/
├── onboard_extended_nodes.py   # CLI: prepare, monitor
├── lib/
│   ├── dnac_client.py          # connect() + credential prompt + shared task-poll helper
│   ├── ise_client.py           # ISE ERS connect() + NDG lookup/remediation
│   ├── hostname_client.py      # Netmiko SSH hostname push
│   ├── csv_loader.py           # CSV -> ExtendedNodeRow, validation
│   ├── resolvers.py            # site / fabric / device ID lookups, cached per run
│   └── port_channels.py        # port channel create + idempotency
├── templates/extended_nodes_template.csv
├── .env.example                 # copy to .env and fill in — see Configuration above
└── logs/                        # per-run results CSVs, gitignored
```

## Verification checklist

Before trusting `--rename-hostname` unattended across a full batch:

1. `pip install -r requirements.txt` to pull in `netmiko`.
2. Run `monitor --csv extended_nodes.csv --rename-hostname --rename-dry-run` first — confirms hostname detection and the unchanged/would-change logic without touching the device.
3. Re-run without `--rename-dry-run` against a single device; confirm on-box via `show run | include hostname` that the name matches the CSV.

Confirmed live: hostname detection, `--rename-dry-run`, and the actual SSH push/save have all been tested successfully against a real controller and device.

