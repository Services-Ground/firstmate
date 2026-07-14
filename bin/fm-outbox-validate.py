#!/usr/bin/env python3
"""Validate a Firstmate Bridge dispatch or outbox result root object."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fm_outbox_contract import ContractError, validate_record


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(description=__doc__)
    out.add_argument("record", type=Path)
    out.add_argument(
        "--projects-file",
        type=Path,
        default=None,
        help="Firstmate data/projects.md registry; defaults to FM_BRIDGE_PROJECTS_FILE or FM_HOME",
    )
    out.add_argument(
        "--status-option",
        action="append",
        default=None,
        help="exact live board status label; repeat for every option",
    )
    return out


def main() -> int:
    args = parser().parse_args()
    try:
        record = json.loads(args.record.read_text(encoding="utf-8"))
        validate_record(
            record,
            status_options=args.status_option,
            projects_file=args.projects_file,
        )
    except (OSError, json.JSONDecodeError, ContractError) as error:
        print(f"invalid: {error}", file=sys.stderr)
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
