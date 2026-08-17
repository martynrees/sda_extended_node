# Extended Node Onboarding Automation

Automates the two manual, error-prone steps required every time an SD-Access
extended node is added to an existing fabric on Cisco Catalyst Center:

1. Creating the **PAgP port-channel** on the fabric edge switch
   (`connectedDeviceType=EXTENDED_NODE`) on the interfaces facing the new
   extended node.
2. **PnP claim** of the extended node's serial/PID to the correct site, which
   triggers Catalyst Center to push the auto-generated extended-node config
   once the device dials home over DHCP.

It does **not** build the fabric, assign IP pools, or configure DHCP — see
[Manual prerequisites](#manual-prerequisites-this-tool-does-not-touch) below.

## Why this is four separate CLI commands, not one script

There's a hard physical gap between the two steps above: the port-channel
must exist *before* the extended node is racked, cabled, and powered on, but
the device won't appear in PnP inventory until after it boots, gets DHCP
(option 43 → Catalyst Center), and calls home. A single "do everything"
script can't bridge a step that requires a human to walk to a rack. Instead,
the tool is four phases you run in sequence, driven by one CSV per
onboarding batch:

```
python onboard_extended_nodes.py portchannels --csv extended_nodes.csv [--dry-run]
# ... now rack, cable, and power on the extended node(s) ...
python onboard_extended_nodes.py status       --csv extended_nodes.csv
python onboard_extended_nodes.py claim        --csv extended_nodes.csv [--dry-run] [--import-if-missing]
python onboard_extended_nodes.py verify       --csv extended_nodes.csv
```

| Phase | Run when | What it does |
|---|---|---|
| `portchannels` | Before racking hardware | Resolves site → fabric → edge device, checks for existing port channels on the requested interfaces (skips + warns instead of erroring if already present), creates the PAgP port channel, polls the task to completion. |
| `status` | After cabling + powering on | Queries PnP by serial number and reports whether the device has dialed home yet. Run this before `claim` to confirm registration. |
| `claim` | Once `status` shows the device has registered | Looks up the PnP device by serial; claims it to its site with `type=Default`; optionally attaches a golden image / Day-0 config if `image_id`/`config_id` are set in the CSV; polls until `Provisioned` or timeout. |
| `verify` | After `claim` completes | Confirms the device shows up in inventory with fabric role `Extended Node` and flags anything that still looks unprovisioned. |

Every phase processes each CSV row independently (one bad row does not abort
the batch), writes a timestamped results CSV to `logs/`, and prints a
succeeded/skipped/failed summary at the end.

## Installation

```bash
git clone <this-repo>
cd extended-node-onboarding
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requires Python 3.8+.

### SDK / controller version pinning

`requirements.txt` pins `dnacentersdk==2.8.14`, which matches Catalyst
Center **2.3.7.9**. If your controller is on a different patch release,
check the [dnacentersdk compatibility
matrix](https://developer.cisco.com/docs/dnac/#!getting-started/sdk-compatibility)
and adjust both the pinned SDK version and the `--cc-version` flag (see
below) to match. Mismatches here are a common source of confusing payload
validation errors rather than a clear "wrong version" message — confirm your
exact controller version under **Settings > About** in the Catalyst Center
UI before running anything against production.

## Credentials

Every run prompts interactively for credentials — nothing sensitive is ever
written to disk:

- **Base URL** and **username**: plain text prompt (or pass `--base-url` /
  `--username` on the command line for convenience — these are not
  secrets).
- **Password**: always `getpass`-masked, never accepted as a CLI argument,
  never logged.

```bash
python onboard_extended_nodes.py portchannels --csv extended_nodes.csv --dry-run
Catalyst Center base URL (e.g. https://10.1.1.1): https://10.1.1.1
Username: admin
Password:
```

Global flags (`--base-url`, `--username`, `--cc-version`, `--no-verify-ssl`)
must come **after** the subcommand name, e.g.
`portchannels --csv extended_nodes.csv --no-verify-ssl` — not before it.
`--no-verify-ssl` disables TLS certificate verification, for self-signed lab
controllers only.

## CSV format

Copy `templates/extended_nodes_template.csv` to your own file (e.g.
`extended_nodes.csv`) and fill in one row per extended node. The template
includes two worked examples and an inline guidance row.

| Column | Required | Notes |
|---|---|---|
| `extended_node_hostname` | yes | Friendly name assigned during claim. |
| `extended_node_serial` | yes | Device serial number. |
| `extended_node_pid` | yes | Product ID, e.g. `IE-3400H-24T`. |
| `site_hierarchy` | yes | Full site path, e.g. `Global/Site1/BuildingA/Level2`. |
| `fabric_edge_identifier` | yes | Hostname or management IP of the fabric edge switch the extended node uplinks to. |
| `edge_port_channel_interfaces` | yes | `;`-separated interface list, e.g. `GigabitEthernet1/0/47;GigabitEthernet1/0/48` (max 8). |
| `port_channel_description` | no | Free text; defaults to `Extended node uplink: <hostname>`. |
| `image_id` | no | Leave blank to skip golden-image assignment during claim. |
| `config_id` | no | Leave blank to skip a Day-0 template during claim. |
| `notes` | no | Ignored by the script — your own tracking column. |

`connectedDeviceType` (`EXTENDED_NODE`) and `protocol` (`PAGP`) are hardcoded
in the tool rather than CSV columns — PAgP is the only protocol Catalyst
Center accepts for an `EXTENDED_NODE` port channel, so exposing it as an
editable option only invites a broken run.

Lines starting with `#` in the first column, and blank lines, are ignored.

## Manual prerequisites (this tool does not touch)

This tool assumes the following are already in place on your fabric:

- The fabric is already built and the target site is already a fabric site.
- An IP pool is already assigned to `INFRA_VN` at the target site.
- DHCP is configured with IP-helper pointing at Catalyst Center, and DHCP
  option 43/82 are intact so the extended node's discovery request reaches
  the controller.
- SNMP is configured at the site (required for Catalyst Center to manage
  the device once it's provisioned).

If any of these are missing, `portchannels` may succeed but the device will
never appear in `status`, or `claim` will succeed but the device will never
reach `Provisioned`.

## Recommended rollout

1. **Dry run**: `portchannels --csv extended_nodes.csv --dry-run` against a
   known lab/test fabric edge device — confirms site/fabric/device
   resolution and payload shape without writing anything.
2. **Single-device test**: run the full four-phase sequence above against
   one spare extended node before touching a full batch. Confirm `verify`
   reports `verified` with `fabric roles=['Extended Node']`.
3. **Batch run**: run all four phases against the full CSV for one site;
   check the results CSV and summary counts in `logs/` match the CSV row
   count.

## Repo layout

```
extended-node-onboarding/
├── README.md
├── requirements.txt
├── .gitignore
├── onboard_extended_nodes.py   # CLI entrypoint (argparse subcommands)
├── lib/
│   ├── dnac_client.py          # connect(), credential prompt, version check
│   ├── csv_loader.py           # load + validate rows -> ExtendedNodeRow
│   ├── resolvers.py            # site_id / fabric_id / edge networkDeviceId lookups (cached per run)
│   ├── port_channels.py        # create + idempotency check + task polling
│   └── pnp.py                  # status lookup, optional import, claim, state polling
├── templates/
│   └── extended_nodes_template.csv
└── logs/                       # created at runtime, gitignored — per-run results CSVs
```
