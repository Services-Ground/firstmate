#!/usr/bin/env python3
"""Validation primitives for the Firstmate Bridge outbox contract v1."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"
ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "record_type",
        "task_id",
        "card_id",
        "repo",
        "mode",
        "brief_path",
        "canonical_thread",
        "pr_url",
        "risk",
        "summary",
        "target_channel_id",
        "target_channel",
        "board_id",
        "new_status",
        "abdul_gated_apply",
    }
)
BASE_REQUIRED = frozenset(
    {"schema_version", "record_type", "task_id", "card_id", "repo", "mode"}
)
FOCALBOARD_ID_RE = re.compile(r"^[a-z0-9]{27}$")
MATTERMOST_ID_RE = re.compile(r"^[a-z0-9]{26}$")
TASK_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
REPO_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
PR_RE = re.compile(
    r"^https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9][0-9]*(?:[/?#][^\s]*)?$"
)


class ContractError(ValueError):
    """A record is not safe to dispatch or relay."""


def default_projects_file() -> Path:
    configured = os.environ.get("FM_BRIDGE_PROJECTS_FILE")
    if configured:
        return Path(configured)
    home = os.environ.get("FM_HOME")
    if home:
        return Path(home) / "data/projects.md"
    return Path("/home/hp/firstmate/data/projects.md")


def registered_repos(projects_file: str | Path | None = None) -> frozenset[str]:
    """Load the exact bridge repo allowlist from the Firstmate project registry."""

    path = Path(projects_file) if projects_file is not None else default_projects_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ContractError(f"project registry is unavailable: {path}") from error
    repos: set[str] = set()
    for line in lines:
        fields = line.split()
        if not fields or fields[0] != "-":
            continue
        if len(fields) < 2 or not REPO_RE.fullmatch(fields[1]):
            raise ContractError(f"project registry has an invalid repo key: {path}")
        repos.add(fields[1])
    if not repos:
        raise ContractError(f"project registry has no registered repos: {path}")
    return frozenset(repos)


def _single_line(value: Any, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    if required and not value.strip():
        raise ContractError(f"{field} must not be empty")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ContractError(f"{field} must be a single line without control characters")
    return value


def validate_record(
    record: Any,
    *,
    status_options: Iterable[str] | None = None,
    projects_file: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and return a Firstmate Bridge root object.

    Board option labels are live board data, so callers handling a board sync must
    pass the labels returned by the board preflight. This intentionally prevents
    a display name or guessed status from reaching Mattermost first.
    """

    if not isinstance(record, dict):
        raise ContractError("record root must be an object")
    unknown = sorted(set(record) - ALLOWED_KEYS)
    if unknown:
        raise ContractError("unknown fields: " + ", ".join(unknown))
    missing = sorted(BASE_REQUIRED - set(record))
    if missing:
        raise ContractError("missing required fields: " + ", ".join(missing))

    if record["schema_version"] != SCHEMA_VERSION:
        raise ContractError(f"schema_version must be {SCHEMA_VERSION}")
    if record["record_type"] not in {"dispatch", "result"}:
        raise ContractError("record_type must be dispatch or result")
    if record["mode"] not in {"ship", "scout"}:
        raise ContractError("mode must be ship or scout")
    task_id = _single_line(record["task_id"], "task_id", required=True)
    card_id = _single_line(record["card_id"], "card_id", required=True)
    if not TASK_RE.fullmatch(task_id):
        raise ContractError("task_id must be a lowercase task slug")
    if not FOCALBOARD_ID_RE.fullmatch(card_id):
        raise ContractError("card_id must be a 27-character Focalboard id")

    for field in (
        "repo",
        "mode",
        "record_type",
        "brief_path",
        "canonical_thread",
        "pr_url",
        "risk",
        "summary",
        "target_channel_id",
        "target_channel",
        "board_id",
        "new_status",
    ):
        if field in record:
            _single_line(record[field], field, required=True)

    if not REPO_RE.fullmatch(record["repo"]):
        raise ContractError("repo must be a safe lowercase project key")
    if record["repo"] not in registered_repos(projects_file):
        raise ContractError("repo is not exactly registered in the Firstmate project registry")

    if "target_channel_id" in record and not MATTERMOST_ID_RE.fullmatch(
        record["target_channel_id"]
    ):
        raise ContractError("target_channel_id must be a 26-character Mattermost id")
    if "board_id" in record and not FOCALBOARD_ID_RE.fullmatch(record["board_id"]):
        raise ContractError("board_id must be a 27-character Focalboard id")

    if "abdul_gated_apply" in record:
        items = record["abdul_gated_apply"]
        if not isinstance(items, list) or not items:
            raise ContractError("abdul_gated_apply must be a non-empty array")
        if len(set(items)) != len(items):
            raise ContractError("abdul_gated_apply entries must be unique")
        for item in items:
            _single_line(item, "abdul_gated_apply item", required=True)

    if record["record_type"] == "dispatch":
        for field in ("brief_path", "canonical_thread"):
            if field not in record:
                raise ContractError(f"dispatch record requires {field}")
    elif record["mode"] == "ship":
        for field in ("pr_url", "risk", "summary"):
            if field not in record:
                raise ContractError(f"ship result requires {field}")
        if not PR_RE.fullmatch(record["pr_url"]):
            raise ContractError("ship result pr_url must be a GitHub pull request URL")
    elif "pr_url" in record:
        raise ContractError("scout result must not contain pr_url")

    has_board = "board_id" in record
    has_status = "new_status" in record
    if has_board != has_status:
        raise ContractError("board_id and new_status must be supplied together")
    if has_status:
        if status_options is None:
            raise ContractError("new_status requires prevalidated live board status options")
        allowed = {str(option) for option in status_options}
        if record["new_status"] not in allowed:
            raise ContractError("new_status is not an exact live board option label")

    return record
