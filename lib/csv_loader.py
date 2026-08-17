"""CSV loading and validation for the extended-node onboarding batch file."""

import csv
from dataclasses import dataclass, field
from typing import List, Optional

REQUIRED_COLUMNS = [
    "extended_node_hostname",
    "extended_node_serial",
    "extended_node_pid",
    "site_hierarchy",
    "fabric_edge_identifier",
    "edge_port_channel_interfaces",
]

MAX_INTERFACES = 8


@dataclass
class ExtendedNodeRow:
    row_number: int  # 1-based, matches spreadsheet row (header = row 1)
    extended_node_hostname: str
    extended_node_serial: str
    extended_node_pid: str
    site_hierarchy: str
    fabric_edge_identifier: str
    edge_port_channel_interfaces: List[str]
    port_channel_description: str = ""
    image_id: Optional[str] = None
    config_id: Optional[str] = None
    notes: str = ""


class CsvValidationError(Exception):
    pass


def _is_comment_row(row: dict) -> bool:
    first_value = next(iter(row.values()), "") or ""
    return first_value.strip().startswith("#")


def load_rows(csv_path: str) -> List[ExtendedNodeRow]:
    """Load and validate the extended-node CSV.

    Raises CsvValidationError with all problems found (not just the first)
    so the customer can fix a batch file in one pass rather than
    one-error-at-a-time.
    """
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing_columns = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing_columns:
            raise CsvValidationError(
                f"CSV is missing required column(s): {', '.join(missing_columns)}"
            )

        rows = []
        errors = []
        for line_number, raw_row in enumerate(reader, start=2):  # header is line 1
            if _is_comment_row(raw_row):
                continue
            if not any((v or "").strip() for v in raw_row.values()):
                continue  # blank line

            row_errors = []
            for col in REQUIRED_COLUMNS:
                if not (raw_row.get(col) or "").strip():
                    row_errors.append(f"row {line_number}: missing required value for '{col}'")

            interfaces_raw = (raw_row.get("edge_port_channel_interfaces") or "").strip()
            interfaces = [i.strip() for i in interfaces_raw.split(";") if i.strip()]
            if interfaces_raw and not interfaces:
                row_errors.append(f"row {line_number}: 'edge_port_channel_interfaces' is empty after parsing")
            if len(interfaces) > MAX_INTERFACES:
                row_errors.append(
                    f"row {line_number}: {len(interfaces)} interfaces given, "
                    f"max {MAX_INTERFACES} allowed per port channel"
                )

            if row_errors:
                errors.extend(row_errors)
                continue

            rows.append(
                ExtendedNodeRow(
                    row_number=line_number,
                    extended_node_hostname=raw_row["extended_node_hostname"].strip(),
                    extended_node_serial=raw_row["extended_node_serial"].strip(),
                    extended_node_pid=raw_row["extended_node_pid"].strip(),
                    site_hierarchy=raw_row["site_hierarchy"].strip(),
                    fabric_edge_identifier=raw_row["fabric_edge_identifier"].strip(),
                    edge_port_channel_interfaces=interfaces,
                    port_channel_description=(raw_row.get("port_channel_description") or "").strip(),
                    image_id=(raw_row.get("image_id") or "").strip() or None,
                    config_id=(raw_row.get("config_id") or "").strip() or None,
                    notes=(raw_row.get("notes") or "").strip(),
                )
            )

        if errors:
            raise CsvValidationError("\n".join(errors))

        if not rows:
            raise CsvValidationError("CSV contains no data rows to process")

        return rows
