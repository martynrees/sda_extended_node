# Extended Node Onboarding Automation

Automates SD-Access extended node onboarding on Cisco Catalyst Center:

1. Creates the PAgP port channel on the fabric edge switch (`connectedDeviceType=EXTENDED_NODE`).
2. Pre-stages the device in PnP (import + claim to site) **before it's even connected.**

Because the device is already claimed, it self-provisions the moment it's racked, cabled, powered on, and dials home over DHCP — no manual PnP claim step per device.

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

## Usage

```bash
# 1. Fill in your CSV
cp templates/extended_nodes_template.csv extended_nodes.csv

# 2. Create port channels + pre-stage/claim PnP for every row (before racking hardware)
python onboard_extended_nodes.py prepare --csv extended_nodes.csv --dry-run   # check first
python onboard_extended_nodes.py prepare --csv extended_nodes.csv

# 3. Rack, cable, power on the extended nodes — any order, any time, no CLI needed

# 4. Check progress — re-run any time, on any subset that's ready
python onboard_extended_nodes.py monitor --csv extended_nodes.csv
```

`monitor` is stateless: run it as many times as you like while a batch comes online. Devices that haven't connected yet just report `not-seen`; devices that have already reached `verified` don't need re-checking.

Add `--debug` to `monitor` to dump the raw PnP / inventory / fabric-role API responses per device to `logs/debug_<timestamp>/<serial>.json` — useful when validating this flow on a controller/version combination it hasn't been tested against yet, or if a device sits at `warning` and you need to see exactly what Catalyst Center returned.

Credentials are always prompted interactively (`--base-url`/`--username` optional as flags, password always via masked `getpass` prompt — never a CLI arg, never logged, never written to disk).

Global flags (`--base-url`, `--username`, `--cc-version`, `--no-verify-ssl`) go **after** the subcommand, e.g. `prepare --csv extended_nodes.csv --no-verify-ssl`.

### `prepare` status values
| Status | Meaning |
|---|---|
| `ok` | Port channel created and/or PnP claimed |
| `skipped` | Port channel and PnP claim both already existed |
| `dry-run` | `--dry-run` was passed, nothing written |
| `failed` | See `detail` — port-channel and PnP errors are reported independently, so one can fail while the other still succeeds |

### `monitor` status values
| Status | Meaning |
|---|---|
| `not-seen` | Device hasn't dialed home yet |
| `in-progress` | Dialed home, still provisioning |
| `verified` | Provisioned and confirmed with fabric role `Extended Node` |
| `warning` | Provisioned, but fabric role doesn't show `Extended Node` — check manually |

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
| `image_id` | no | Golden image to assign on claim; blank to skip |
| `config_id` | no | Day-0 template to assign on claim; blank to skip |
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
│   ├── dnac_client.py          # connect() + credential prompt
│   ├── csv_loader.py           # CSV -> ExtendedNodeRow, validation
│   ├── resolvers.py            # site / fabric / device ID lookups, cached per run
│   ├── port_channels.py        # port channel create + idempotency + task polling
│   └── pnp.py                  # PnP import + claim (pre-stage)
├── templates/extended_nodes_template.csv
└── logs/                        # per-run results CSVs, gitignored
```
