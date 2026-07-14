#!/usr/bin/env python3
"""Canonical deployable Phase A relay for versioned Firstmate results.

This source is installed into Hermes only during the Abdul-gated apply. It
posts as Ron, syncs Focalboard as Amina, never merges, and treats the outbox as
an untrusted boundary.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from fm_outbox_contract import ContractError, FOCALBOARD_ID_RE, validate_record

STATUS_PROP = "sg_status"
DEFAULT_CHANNEL_KEY = "agentic-development"
MATTERMOST_ID = re.compile(r"^[a-z0-9]{26}$")


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


@contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Relay:
    def __init__(self) -> None:
        home = Path(os.environ.get("FM_RELAY_HERMES_HOME", "/home/hp/.hermes"))
        self.outbox = Path(os.environ.get("FM_RELAY_OUTBOX", "/home/hp/firstmate/data/outbox"))
        self.done = Path(os.environ.get("FM_RELAY_DONE", str(self.outbox / "done")))
        self.error = Path(os.environ.get("FM_RELAY_ERROR", str(self.outbox / "error")))
        self.state = Path(os.environ.get("FM_RELAY_STATE", str(home / "sg-ops/firstmate-relay")))
        self.ledger = Path(
            os.environ.get("FM_RELAY_LEDGER", str(home / "sg-ops/firstmate-relay-ledger.jsonl"))
        )
        self.policy_path = Path(
            os.environ.get("FM_RELAY_POLICY", str(home / "sg-ops/agent-channel-policy.json"))
        )
        self.dispatch_ledger = Path(
            os.environ.get(
                "FM_BRIDGE_DISPATCH_LEDGER",
                "/home/hp/firstmate/state/bridge/dispatch-ledger.jsonl",
            )
        )
        firstmate_home = Path(os.environ.get("FM_HOME", "/home/hp/firstmate"))
        self.projects_file = Path(
            os.environ.get(
                "FM_BRIDGE_PROJECTS_FILE",
                str(firstmate_home / "data/projects.md"),
            )
        )
        base_env = load_env(home / ".env")
        amina_env = {**base_env, **load_env(home / "profiles/amina/.env")}
        self.base = (os.environ.get("FM_RELAY_BASE_URL") or base_env.get("MATTERMOST_URL") or "").rstrip("/")
        self.ron_token = os.environ.get("FM_RELAY_RON_TOKEN") or base_env.get("MATTERMOST_TOKEN") or ""
        self.amina_token = (
            os.environ.get("FM_RELAY_AMINA_TOKEN")
            or amina_env.get("MATTERMOST_TOKEN")
            or ""
        )
        self.policy = self._load_json(self.policy_path, {})

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def headers(self, *, amina: bool = False) -> dict[str, str]:
        token = self.amina_token if amina else self.ron_token
        return {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }

    def api(
        self,
        method: str,
        path: str,
        data: Any = None,
        *,
        amina: bool = False,
        timeout: int = 40,
    ) -> tuple[int, Any]:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers=self.headers(amina=amina),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            try:
                value = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                value = raw[:1200]
            return error.code, value
        except Exception as error:  # network boundary, recorded without credentials
            return 0, f"{type(error).__name__}: {error}"

    def append_ledger(self, row: dict[str, Any]) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def resolve_channel(self, record: dict[str, Any]) -> tuple[str, str]:
        channel_id = record.get("target_channel_id")
        if channel_id:
            return str(channel_id), "target_channel_id"
        channels = self.policy.get("pilot_channels") or {}
        name = str(record.get("target_channel") or "").strip()
        if name:
            normalized = name.lower().replace("_", "-").strip()
            for key, channel in channels.items():
                display = str(channel.get("display_name") or "")
                if normalized in {
                    key.lower(),
                    display.lower(),
                    display.lower().replace(" ", "-"),
                }:
                    resolved = str(channel.get("channel_id") or "")
                    if MATTERMOST_ID.fullmatch(resolved):
                        return resolved, f"policy:{key}"
            slug = re.sub(r"[^a-z0-9-]+", "-", normalized).strip("-")
            team_id = str(self.policy.get("team_id") or "")
            if not team_id:
                raise ContractError("policy file is missing team_id; cannot resolve target_channel by name")
            status, value = self.api(
                "GET", f"/api/v4/teams/{team_id}/channels/name/{urllib.parse.quote(slug)}"
            )
            if status == 200 and isinstance(value, dict) and MATTERMOST_ID.fullmatch(str(value.get("id") or "")):
                return str(value["id"]), f"mattermost:{slug}"
            raise ContractError(f"target_channel cannot be resolved exactly: {name}")
        default = str((channels.get(DEFAULT_CHANNEL_KEY) or {}).get("channel_id") or "")
        if not MATTERMOST_ID.fullmatch(default):
            raise ContractError("Agentic Development fallback channel is not configured")
        return default, "fallback:agentic-development"

    @staticmethod
    def board_option_matches(board: dict[str, Any], wanted: str) -> list[tuple[str, str]]:
        matches: list[tuple[str, str]] = []
        for prop in board.get("cardProperties") or []:
            property_id = str(prop.get("id") or "")
            for option in prop.get("options") or []:
                if str(option.get("value") or "") == wanted and property_id and option.get("id"):
                    matches.append((property_id, str(option["id"])))
        return matches

    @staticmethod
    def board_option_labels(board: dict[str, Any]) -> set[str]:
        return {
            str(option["value"])
            for prop in board.get("cardProperties") or []
            for option in prop.get("options") or []
            if option.get("value") and option.get("id")
        }

    @staticmethod
    def property_option_value(board: dict[str, Any], property_id: str, option_id: str) -> str:
        for prop in board.get("cardProperties") or []:
            if prop.get("id") != property_id:
                continue
            for option in prop.get("options") or []:
                if option.get("id") == option_id:
                    return str(option.get("value") or "")
        return ""

    def preflight_card(self, record: dict[str, Any]) -> dict[str, Any] | None:
        if "board_id" not in record:
            validate_record(record, projects_file=self.projects_file)
            return None
        # First reject malformed roots and display-name ids without trusting a
        # caller-provided status. The real option set is checked after the GETs.
        validate_record(
            record,
            status_options=[str(record.get("new_status") or "")],
            projects_file=self.projects_file,
        )
        board_id = str(record["board_id"])
        card_id = str(record["card_id"])
        status, board = self.api("GET", f"/plugins/focalboard/api/v2/boards/{board_id}", amina=True)
        if status != 200 or not isinstance(board, dict):
            raise ContractError(f"board preflight failed: HTTP {status}")
        status, blocks = self.api(
            "GET", f"/plugins/focalboard/api/v2/boards/{board_id}/blocks", amina=True
        )
        if status != 200 or not isinstance(blocks, list):
            raise ContractError(f"card preflight failed: HTTP {status}")
        card = next(
            (
                block
                for block in blocks
                if block.get("id") == card_id
                and block.get("type") == "card"
                and not block.get("deleteAt")
            ),
            None,
        )
        if not card:
            raise ContractError("card preflight failed: card not found")
        validate_record(
            record,
            status_options=self.board_option_labels(board),
            projects_file=self.projects_file,
        )
        matches = self.board_option_matches(board, str(record["new_status"]))
        if not matches:
            raise ContractError("card preflight failed: status option not found")
        if len(matches) != 1:
            raise ContractError("card preflight failed: status option label is ambiguous")
        property_id, status_id = matches[0]

        property_updates = {property_id: status_id}
        reviewer_label = "card owner"
        properties = ((card.get("fields") or {}).get("properties") or {})
        # Department Driver boards keep the visual lane in sg_stage and the
        # execution state in sg_status. A QA handoff must update both and hand
        # the Holder person field to the named human reviewer.
        if property_id == "sg_stage" and record["new_status"] == "QA / Review":
            reviewing = self.board_option_matches(board, "Human Reviewing")
            reviewing = [match for match in reviewing if match[0] == STATUS_PROP]
            if len(reviewing) != 1:
                raise ContractError("card preflight failed: Human Reviewing workflow option missing")
            property_updates[STATUS_PROP] = reviewing[0][1]
            reviewer_option = str(properties.get("sg_human_reviewer") or "")
            reviewer_label = self.property_option_value(board, "sg_human_reviewer", reviewer_option)
            if not reviewer_label:
                raise ContractError("card preflight failed: human reviewer is missing or unknown")
            username = reviewer_option.strip().lower()
            if not re.fullmatch(r"[a-z0-9._-]+", username):
                raise ContractError("card preflight failed: human reviewer username is invalid")
            status, user = self.api(
                "GET",
                f"/api/v4/users/username/{urllib.parse.quote(username)}",
                amina=True,
            )
            reviewer_id = str(user.get("id") or "") if isinstance(user, dict) else ""
            if status != 200 or not MATTERMOST_ID.fullmatch(reviewer_id):
                raise ContractError("card preflight failed: human reviewer account cannot be resolved")
            property_updates["sg_owner"] = reviewer_id
        return {
            "board": board,
            "blocks": blocks,
            "card": card,
            "status_id": status_id,
            "property_updates": property_updates,
            "reviewer_label": reviewer_label,
        }

    @staticmethod
    def delivery_key(record: dict[str, Any]) -> str:
        raw = "|".join(
            [
                str(record["schema_version"]),
                str(record["task_id"]),
                str(record["card_id"]),
                str(record.get("pr_url") or "scout"),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def marker_path(self, key: str) -> Path:
        return self.state / "deliveries" / f"{key}.json"

    def post_channel(self, channel_id: str, record: dict[str, Any], key: str) -> str:
        message = (
            f"PR: {record['pr_url']} · Risk: {record['risk']} · {record['summary']}\n\n"
            "Merge remains Abdul's gate."
        )
        payload = {
            "channel_id": channel_id,
            "message": message,
            # Mattermost treats pending_post_id as a client idempotency key.
            "pending_post_id": f"firstmate:{key}",
        }
        status, value = self.api("POST", "/api/v4/posts", payload)
        if status not in (200, 201) or not isinstance(value, dict) or not value.get("id"):
            raise RuntimeError(f"mattermost post failed: HTTP {status}: {value}")
        return str(value["id"])

    @staticmethod
    def comment_marker(key: str) -> str:
        return f"FIRSTMATE-RELAY:{key}"

    def sync_card(
        self,
        record: dict[str, Any],
        preflight: dict[str, Any] | None,
        post_id: str,
        key: str,
    ) -> str:
        if preflight is None:
            return "no-card-sync"
        board_id = str(record["board_id"])
        card_id = str(record["card_id"])
        blocks = preflight["blocks"]
        marker = self.comment_marker(key)
        comment_block = next(
            (
                block
                for block in blocks
                if block.get("parentId") == card_id
                and marker in str(block.get("title") or "")
                and not block.get("deleteAt")
            ),
            None,
        )
        comment_exists = comment_block is not None
        comment_id = str(comment_block.get("id") or "") if comment_block else "fm" + key[:25]
        if not FOCALBOARD_ID_RE.fullmatch(comment_id):
            raise RuntimeError("Focalboard comment id is not canonical")
        if not comment_exists:
            comment = {
                "id": comment_id,
                "parentId": card_id,
                "boardId": board_id,
                "schema": 1,
                "type": "text",
                "title": "\n".join(
                    [
                        f"Firstmate result relay ({now_iso()})",
                        marker,
                        f"PR: {record['pr_url']}",
                        f"Risk: {record['risk']}",
                        f"Summary: {record['summary']}",
                        f"QA: {preflight['reviewer_label']} checks the PR against the card acceptance criteria; do not merge without Abdul gate.",
                        f"Mattermost post: {post_id}",
                    ]
                ),
                "createdBy": "",
                "modifiedBy": "",
                "createAt": int(time.time() * 1000),
                "updateAt": int(time.time() * 1000),
                "deleteAt": 0,
                "fields": {},
            }
            status, value = self.api(
                "POST",
                f"/plugins/focalboard/api/v2/boards/{board_id}/blocks",
                [comment],
                amina=True,
            )
            created_ids = (
                {
                    str(block.get("id") or "")
                    for block in value
                    if isinstance(block, dict)
                }
                if isinstance(value, list)
                else set()
            )
            if status not in (200, 201) or comment_id not in created_ids:
                raise RuntimeError(f"Focalboard comment failed: HTTP {status}: {value}")

        # Focalboard replaces updatedFields.properties. Copy the full current
        # map, change only the status entry, and retain every unrelated field.
        card = dict(preflight["card"])
        fields = dict(card.get("fields") or {})
        properties = dict(fields.get("properties") or {})
        properties.update(preflight["property_updates"])
        content_order = list(fields.get("contentOrder") or [])
        if comment_id not in content_order:
            content_order.append(comment_id)
        updated_fields = {"properties": properties, "contentOrder": content_order}
        status, value = self.api(
            "PATCH",
            f"/plugins/focalboard/api/v2/boards/{board_id}/blocks/{card_id}",
            {"updatedFields": updated_fields},
            amina=True,
        )
        if status not in (200, 201):
            raise RuntimeError(f"Focalboard status update failed: HTTP {status}: {value}")

        # Focalboard can return HTTP 200 while ignoring an invalid option or
        # partial block payload. Never record done without reading back every
        # required property and the visible comment ordering.
        status, verified_blocks = self.api(
            "GET", f"/plugins/focalboard/api/v2/boards/{board_id}/blocks", amina=True
        )
        if status != 200 or not isinstance(verified_blocks, list):
            raise RuntimeError(f"Focalboard verification failed: HTTP {status}")
        verified_card = next(
            (
                block
                for block in verified_blocks
                if block.get("id") == card_id
                and block.get("type") == "card"
                and not block.get("deleteAt")
            ),
            None,
        )
        verified_properties = (
            ((verified_card or {}).get("fields") or {}).get("properties") or {}
        )
        verified_order = ((verified_card or {}).get("fields") or {}).get("contentOrder") or []
        verified_comment = any(
            block.get("id") == comment_id
            and block.get("parentId") == card_id
            and marker in str(block.get("title") or "")
            and not block.get("deleteAt")
            for block in verified_blocks
        )
        if (
            not verified_card
            or any(
                verified_properties.get(name) != expected
                for name, expected in preflight["property_updates"].items()
            )
            or comment_id not in verified_order
            or not verified_comment
        ):
            raise RuntimeError("Focalboard verification failed: required handoff state did not persist")
        return f"comment={'existing' if comment_exists else 'created'};status={preflight['status_id']}"

    @staticmethod
    def move_unique(source: Path, destination_dir: Path) -> Path:
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name
        if destination.exists():
            destination = destination_dir / f"{source.stem}-{int(time.time() * 1000)}{source.suffix}"
        shutil.move(str(source), str(destination))
        return destination

    def mark_dispatch_completed(self, record: dict[str, Any]) -> None:
        self.dispatch_ledger.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.dispatch_ledger.with_suffix(".lock")
        with locked(lock_path):
            with self.dispatch_ledger.open("a", encoding="utf-8") as handle:
                row = {
                    "at": now_iso(),
                    "state": "completed",
                    "card_id": record["card_id"],
                    "repo": record["repo"],
                    "mode": record["mode"],
                    "target": "relay",
                    "digest": self.delivery_key(record),
                    "transport": "relay-completed",
                    "retry_decision": "none",
                    "reason": "outbox result delivered",
                }
                handle.write(json.dumps(row, sort_keys=True) + "\n")

    def process_file(self, path: Path) -> dict[str, Any]:
        row: dict[str, Any] = {"at": now_iso(), "file": str(path), "action": "process"}
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            # Reject arrays and malformed ids before any network write.
            if isinstance(record, dict) and record.get("new_status"):
                validate_record(
                    record,
                    status_options=[str(record["new_status"])],
                    projects_file=self.projects_file,
                )
            else:
                validate_record(record, projects_file=self.projects_file)
            if record["record_type"] != "result":
                raise ContractError("relay accepts result records only")
            key = self.delivery_key(record)
            row["delivery_key"] = key
            if record["mode"] == "scout":
                self.mark_dispatch_completed(record)
                moved = self.move_unique(path, self.done)
                row.update({"status": "skipped-scout", "moved_to": str(moved)})
                self.append_ledger(row)
                return row

            preflight = self.preflight_card(record)
            channel_id, channel_source = self.resolve_channel(record)
            row.update({"channel_id": channel_id, "channel_source": channel_source})
            marker_path = self.marker_path(key)
            marker = self._load_json(marker_path, {})
            if marker.get("status") == "completed":
                moved = self.move_unique(path, self.done)
                row.update(
                    {
                        "status": "deduped",
                        "post_ref": marker.get("post_id"),
                        "moved_to": str(moved),
                    }
                )
                self.append_ledger(row)
                return row

            post_id = str(marker.get("post_id") or "")
            if not post_id:
                post_id = self.post_channel(channel_id, record, key)
                marker = {
                    "schema_version": "1.0",
                    "delivery_key": key,
                    "status": "posted",
                    "post_id": post_id,
                    "posted_at": now_iso(),
                }
                atomic_json(marker_path, marker)
            card_result = self.sync_card(record, preflight, post_id, key)
            # Release the Phase A one-card claim before declaring the delivery
            # marker completed. If this append fails, the marker stays at
            # "posted" and a retry resumes without another Mattermost post.
            self.mark_dispatch_completed(record)
            marker.update({"status": "completed", "completed_at": now_iso(), "card_result": card_result})
            atomic_json(marker_path, marker)
            moved = self.move_unique(path, self.done)
            row.update(
                {
                    "status": "done",
                    "post_ref": post_id,
                    "card_ref": card_result,
                    "board_id": record.get("board_id"),
                    "card_id": record["card_id"],
                    "moved_to": str(moved),
                }
            )
        except Exception as error:
            moved = self.move_unique(path, self.error)
            row.update(
                {
                    "status": "error",
                    "error": f"{type(error).__name__}: {error}",
                    "moved_to": str(moved),
                }
            )
        self.append_ledger(row)
        return row

    def run(self) -> list[dict[str, Any]]:
        if not self.base or not self.ron_token or not self.amina_token:
            raise RuntimeError("relay requires Mattermost base URL plus Ron and Amina tokens")
        self.outbox.mkdir(parents=True, exist_ok=True)
        with locked(self.state / "relay.lock"):
            return [self.process_file(path) for path in sorted(self.outbox.glob("*.json")) if path.is_file()]


def main() -> int:
    try:
        results = Relay().run()
    except Exception as error:
        print(f"sg-firstmate-relay: {error}", file=sys.stderr)
        return 2
    if results:
        print(json.dumps({"processed": len(results), "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
