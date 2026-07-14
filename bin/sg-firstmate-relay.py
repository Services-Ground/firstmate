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

from fm_outbox_contract import ContractError, validate_record

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
    def board_status_options(board: dict[str, Any]) -> tuple[dict[str, str], str]:
        for prop in board.get("cardProperties") or []:
            if prop.get("id") == STATUS_PROP or str(prop.get("name") or "").lower() == "status":
                options = {
                    str(option.get("value")): str(option.get("id"))
                    for option in prop.get("options") or []
                    if option.get("value") and option.get("id")
                }
                return options, str(prop.get("id") or STATUS_PROP)
        return {}, STATUS_PROP

    def preflight_card(self, record: dict[str, Any]) -> dict[str, Any] | None:
        if "board_id" not in record:
            validate_record(record)
            return None
        # First reject malformed roots and display-name ids without trusting a
        # caller-provided status. The real option set is checked after the GETs.
        validate_record(record, status_options=[str(record.get("new_status") or "")])
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
        options, property_id = self.board_status_options(board)
        validate_record(record, status_options=options)
        return {
            "board": board,
            "blocks": blocks,
            "card": card,
            "status_id": options[record["new_status"]],
            "status_property_id": property_id,
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
        comment_exists = any(
            block.get("parentId") == card_id
            and marker in str(block.get("title") or "")
            and not block.get("deleteAt")
            for block in blocks
        )
        if not comment_exists:
            comment_id = "fm" + key[:25]
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
                        "QA: check the PR against the card acceptance criteria; do not merge without Abdul gate.",
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
            if status not in (200, 201):
                raise RuntimeError(f"Focalboard comment failed: HTTP {status}: {value}")

        # Focalboard replaces updatedFields.properties. Copy the full current
        # map, change only the status entry, and retain every unrelated field.
        card = dict(preflight["card"])
        fields = dict(card.get("fields") or {})
        properties = dict(fields.get("properties") or {})
        properties[preflight["status_property_id"]] = preflight["status_id"]
        fields["properties"] = properties
        card["fields"] = fields
        card["updateAt"] = int(time.time() * 1000)
        status, value = self.api(
            "PATCH",
            f"/plugins/focalboard/api/v2/boards/{board_id}/blocks/{card_id}",
            card,
            amina=True,
        )
        if status not in (200, 201):
            raise RuntimeError(f"Focalboard status update failed: HTTP {status}: {value}")
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
                validate_record(record, status_options=[str(record["new_status"])])
            else:
                validate_record(record)
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
