#!/usr/bin/env python3
"""Kenza-triggered, one-card Phase A Department Driver adapter.

This is deliberately not a scheduler. Kenza supplies one canonical card id; the
adapter reuses the Department Driver gate, envelope, owner WIP, task dedupe,
idempotency-key, lock, and max-one rules before calling the strict injector once.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

STATUS_READY = "Ready for AI"
STATUS_WORKING = "AI Working"
STATUS_FIELD = "sg_status"
REQUIRED_ENVELOPE = {
    "sg_ai_owner": "AI Owner",
    "sg_human_reviewer": "Human Reviewer",
    "sg_canonical_thread": "Canonical Thread",
    "sg_next_action": "Next Action",
    "sg_blocker": "Blocker",
    "sg_acceptance_criteria": "Acceptance Criteria",
    "sg_work_type": "Work Type",
}
CODE_TYPES = {"code", "dev", "development", "build", "engineering"}
TERMINAL_TASK_STATES = {"done", "completed", "archived", "cancelled", "failed"}


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def props(card: dict[str, Any]) -> dict[str, Any]:
    return dict((card.get("fields") or {}).get("properties") or {})


def run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-id", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--brief-path", required=True)
    parser.add_argument("--board-id", required=True)
    parser.add_argument("--mode", choices=("ship", "scout"), default="ship")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--target-channel")
    target.add_argument("--target-channel-id")
    parser.add_argument("--new-status", default="QA / Review")
    parser.add_argument("--lane", default="bull-and-bear")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--max", type=int, default=1)
    parser.add_argument(
        "--gate-command",
        default=os.environ.get(
            "FM_BRIDGE_DRIVER_GATE",
            "/mnt/c/Users/HP/Claude/Projects/Agentic Engineering/command-center/00-setup/sg_driver_gate.py",
        ),
    )
    parser.add_argument(
        "--injector",
        default=str(Path(__file__).with_name("fm-bridge-inject.sh")),
    )
    return parser.parse_args()


class Trigger:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        hermes_home = Path(os.environ.get("HERMES_HOME", "/home/hp/.hermes"))
        load_env(hermes_home / ".env")
        amina_env = hermes_home / "profiles/amina/.env"
        profile_values: dict[str, str] = {}
        if amina_env.exists():
            for line in amina_env.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    profile_values[key.strip()] = value.strip().strip('"').strip("'")
        self.base = os.environ.get("FM_BRIDGE_BASE_URL") or os.environ.get("MATTERMOST_URL", "")
        self.token = (
            os.environ.get("FM_BRIDGE_AMINA_TOKEN")
            or profile_values.get("MATTERMOST_TOKEN")
            or os.environ.get("MATTERMOST_TOKEN", "")
        )
        self.state_dir = Path(
            os.environ.get("FM_BRIDGE_TRIGGER_STATE", str(hermes_home / "sg-ops/firstmate-bridge-trigger"))
        )
        self.kenza_values = {
            value.strip().lower()
            for value in os.environ.get("FM_BRIDGE_KENZA_VALUES", "kenza").split(",")
            if value.strip()
        }

    def api(self, method: str, path: str, data: Any = None) -> tuple[int, Any]:
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(
            self.base.rstrip("/") + path,
            data=body,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read().decode("utf-8", "replace")
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")[:1200]
        except Exception as error:
            return 0, f"{type(error).__name__}: {error}"

    @staticmethod
    def status_options(board: dict[str, Any]) -> tuple[dict[str, str], str]:
        for prop in board.get("cardProperties") or []:
            if prop.get("id") == STATUS_FIELD or str(prop.get("name") or "").lower() == "status":
                return (
                    {
                        str(option.get("value")): str(option.get("id"))
                        for option in prop.get("options") or []
                        if option.get("value") and option.get("id")
                    },
                    str(prop.get("id") or STATUS_FIELD),
                )
        return {}, STATUS_FIELD

    def gate(self) -> None:
        command = [
            self.args.gate_command,
            "--board-id",
            self.args.board_id,
            "--lane",
            self.args.lane,
            "--json",
        ]
        result = run(command)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Department Driver gate did not return JSON: {error}") from error
        if result.returncode not in (0, 2) or not payload.get("ok"):
            raise RuntimeError("Department Driver gate is RED")

    @staticmethod
    def task_exists_for_card(card_id: str) -> bool:
        result = run(["hermes", "kanban", "list", "--json"], timeout=60)
        if result.returncode != 0:
            raise RuntimeError("cannot read Hermes Kanban task state")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            text = result.stdout + "\n" + result.stderr
            return card_id in text
        rows = payload if isinstance(payload, list) else payload.get("tasks", [])
        for row in rows:
            blob = json.dumps(row, sort_keys=True)
            state = str(row.get("status") or row.get("state") or "").lower()
            if card_id in blob and state not in TERMINAL_TASK_STATES:
                return True
        return False

    def fetch_board(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        status, board = self.api(
            "GET", f"/plugins/focalboard/api/v2/boards/{self.args.board_id}"
        )
        if status != 200 or not isinstance(board, dict):
            raise RuntimeError(f"board fetch failed: HTTP {status}")
        status, blocks = self.api(
            "GET", f"/plugins/focalboard/api/v2/boards/{self.args.board_id}/blocks"
        )
        if status != 200 or not isinstance(blocks, list):
            raise RuntimeError(f"board blocks fetch failed: HTTP {status}")
        return board, blocks

    def eligible_card(
        self, board: dict[str, Any], blocks: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, str], str]:
        card = next(
            (
                block
                for block in blocks
                if block.get("id") == self.args.card_id
                and block.get("type") == "card"
                and not block.get("deleteAt")
            ),
            None,
        )
        if not card:
            raise RuntimeError("canonical card not found")
        values = props(card)
        missing = [label for field, label in REQUIRED_ENVELOPE.items() if not str(values.get(field) or "").strip()]
        if missing:
            raise RuntimeError("structured envelope is incomplete: " + ", ".join(missing))
        options, property_id = self.status_options(board)
        ready_id = options.get(STATUS_READY)
        working_id = options.get(STATUS_WORKING)
        if not ready_id or not working_id or self.args.new_status not in options:
            raise RuntimeError("required live status options are absent")
        if values.get(property_id) != ready_id:
            raise RuntimeError("card status is not Ready for AI")
        owner = str(values.get("sg_ai_owner") or "").strip().lower()
        if owner not in self.kenza_values:
            raise RuntimeError("AI Owner is not Kenza")
        if str(values.get("sg_work_type") or "").strip().lower() not in CODE_TYPES:
            raise RuntimeError("card is not explicitly a code/build work type")
        for other in blocks:
            if other.get("type") != "card" or other.get("deleteAt") or other.get("id") == card.get("id"):
                continue
            other_values = props(other)
            if (
                str(other_values.get("sg_ai_owner") or "").strip().lower() in self.kenza_values
                and other_values.get(property_id) == working_id
            ):
                raise RuntimeError("Kenza already has an active AI Working card")
        return card, options, property_id

    def call_injector_once(
        self,
        card: dict[str, Any],
        options: dict[str, str],
    ) -> dict[str, Any]:
        values = props(card)
        command = [
            self.args.injector,
            "--repo",
            self.args.repo,
            "--brief-path",
            self.args.brief_path,
            "--card-id",
            self.args.card_id,
            "--mode",
            self.args.mode,
            "--canonical-thread",
            str(values["sg_canonical_thread"]),
            "--board-id",
            self.args.board_id,
            "--new-status",
            self.args.new_status,
        ]
        if self.args.target_channel_id:
            command.extend(["--target-channel-id", self.args.target_channel_id])
        else:
            command.extend(["--target-channel", self.args.target_channel])
        for label in options:
            command.extend(["--status-option", label])
        result = run(command)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"injector did not return JSON: {error}: {result.stderr[-500:]}") from error
        if payload.get("state") != "sent":
            raise RuntimeError(f"injector did not durably send: {payload.get('state')}")
        return payload

    def move_to_working(
        self,
        card: dict[str, Any],
        options: dict[str, str],
        property_id: str,
    ) -> None:
        updated = dict(card)
        fields = dict(updated.get("fields") or {})
        properties = dict(fields.get("properties") or {})
        properties[property_id] = options[STATUS_WORKING]
        fields["properties"] = properties
        updated["fields"] = fields
        status, value = self.api(
            "PATCH",
            f"/plugins/focalboard/api/v2/boards/{self.args.board_id}/blocks/{self.args.card_id}",
            updated,
        )
        if status not in (200, 201):
            raise RuntimeError(f"AI Working update failed: HTTP {status}: {value}")

    def execute(self) -> dict[str, Any]:
        if not self.base or not self.token:
            raise RuntimeError("Mattermost/Focalboard URL and Amina token are required")
        if self.args.max != 1:
            raise RuntimeError("Phase A requires --max 1")
        expected_key = f"firstmate-bridge:{self.args.card_id}"
        idempotency_key = self.args.idempotency_key or expected_key
        if idempotency_key != expected_key:
            raise RuntimeError("idempotency key must be card-keyed")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_dir / "trigger.lock"
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self.gate()
            if self.task_exists_for_card(self.args.card_id):
                return {
                    "state": "deduped-existing-task",
                    "card_id": self.args.card_id,
                    "idempotency_key": idempotency_key,
                    "max": 1,
                }
            board, blocks = self.fetch_board()
            card, options, property_id = self.eligible_card(board, blocks)
            result = self.call_injector_once(card, options)
            if result.get("state") != "sent":
                raise RuntimeError("card cannot move before durable injector sent")
            self.move_to_working(card, options, property_id)
            state = {
                "state": "sent",
                "card_id": self.args.card_id,
                "idempotency_key": idempotency_key,
                "max": 1,
                "injector": result,
            }
            temp = self.state_dir / f".{self.args.card_id}.{os.getpid()}.tmp"
            temp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temp, self.state_dir / f"{self.args.card_id}.json")
            return state


def main() -> int:
    try:
        result = Trigger(parse_args()).execute()
    except Exception as error:
        print(f"sg-firstmate-kenza-trigger: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
