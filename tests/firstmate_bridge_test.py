#!/usr/bin/env python3
"""Unit/integration coverage for the Phase A contract, relay, and trigger."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from fm_outbox_contract import ContractError, validate_record  # noqa: E402


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay_module = load_script("sg_firstmate_relay", "sg-firstmate-relay.py")
trigger_module = load_script("sg_firstmate_trigger", "sg-firstmate-kenza-trigger.py")

CARD = "a" * 27
BOARD = "b" * 27
CHANNEL = "c" * 26


def ship_record(**updates):
    record = {
        "schema_version": "1.0",
        "record_type": "result",
        "task_id": "bridge-test",
        "card_id": CARD,
        "repo": "firstmate",
        "mode": "ship",
        "pr_url": "https://github.com/Services-Ground/firstmate/pull/123",
        "risk": "low",
        "summary": "Phase A bridge is ready.",
        "target_channel_id": CHANNEL,
        "board_id": BOARD,
        "new_status": "QA / Review",
    }
    record.update(updates)
    return record


class ContractTests(unittest.TestCase):
    def test_valid_ship_and_dispatch(self):
        validate_record(ship_record(), status_options=["QA / Review"])
        validate_record(
            {
                "schema_version": "1.0",
                "record_type": "dispatch",
                "task_id": "bridge-test",
                "card_id": CARD,
                "repo": "lead-ops-agent",
                "mode": "scout",
                "brief_path": "/tmp/brief.md",
                "canonical_thread": "thread-1",
            }
        )

    def assert_rejected(self, record, options=None):
        with self.assertRaises(ContractError):
            validate_record(record, status_options=options)

    def test_rejection_matrix(self):
        self.assert_rejected([ship_record()], ["QA / Review"])
        self.assert_rejected({**ship_record(), "board_id": "Lead Ops App"}, ["QA / Review"])
        missing_pr = ship_record()
        del missing_pr["pr_url"]
        self.assert_rejected(missing_pr, ["QA / Review"])
        self.assert_rejected({**ship_record(), "pr_url": "https://example.test/pull/1"}, ["QA / Review"])
        self.assert_rejected({**ship_record(), "summary": "line one\nline two"}, ["QA / Review"])
        self.assert_rejected({**ship_record(), "risk": "low\x07"}, ["QA / Review"])
        self.assert_rejected({**ship_record(), "repo": "symbol_lookup"}, ["QA / Review"])
        self.assert_rejected(ship_record(), ["Done"])
        self.assert_rejected({**ship_record(), "target_channel_id": "short"}, ["QA / Review"])
        self.assert_rejected({**ship_record(), "card_id": "a" * 26}, ["QA / Review"])
        self.assert_rejected({**ship_record(), "board_id": "b" * 26}, ["QA / Review"])
        self.assert_rejected({**ship_record(), "extra": "alias"}, ["QA / Review"])

    def test_scout_is_explicitly_non_pr(self):
        scout = ship_record(mode="scout")
        del scout["pr_url"]
        validate_record(scout, status_options=["QA / Review"])
        self.assert_rejected({**scout, "pr_url": "https://github.com/a/b/pull/1"}, ["QA / Review"])


class FakeRelay(relay_module.Relay):
    def __init__(self, root: Path):
        self.outbox = root / "outbox"
        self.done = self.outbox / "done"
        self.error = self.outbox / "error"
        self.state = root / "state"
        self.ledger = root / "relay.jsonl"
        self.policy_path = root / "policy.json"
        self.dispatch_ledger = root / "dispatch.jsonl"
        self.base = "https://mattermost.test"
        self.ron_token = "ron-test-token"
        self.amina_token = "amina-test-token"
        self.policy = {
            "pilot_channels": {
                "agentic-development": {
                    "channel_id": CHANNEL,
                    "display_name": "Agentic Development",
                }
            }
        }
        self.outbox.mkdir(parents=True)
        self.posts = []
        self.comments = []
        self.patches = []
        self.board_ok = True
        self.fail_patch_once = False
        self.ignore_patch_once = False
        self.reviewer_id = "r" * 26
        self.card = {
            "id": CARD,
            "type": "card",
            "deleteAt": 0,
            "title": "Bridge card",
            "fields": {
                "properties": {
                    "sg_status": "sg_status_ai_working",
                    "sg_stage": "sg_stage_in_progress",
                    "sg_ai_owner": "kenza",
                    "sg_human_reviewer": "romman",
                    "sg_owner": "k" * 26,
                    "sg_priority": "sg_prio_high",
                    "unrelated": "preserve-me",
                },
                "contentOrder": ["text-1"],
            },
        }

    def api(self, method, path, data=None, *, amina=False, timeout=40):
        if method == "GET" and path.endswith(f"/boards/{BOARD}"):
            if not self.board_ok:
                return 404, {"message": "missing"}
            return 200, {
                "id": BOARD,
                "cardProperties": [
                    {
                        "id": "sg_status",
                        "name": "Status",
                        "options": [
                            {"id": "sg_status_ready_ai", "value": "Ready for AI"},
                            {"id": "sg_status_ai_working", "value": "AI Working"},
                            {"id": "sg_status_human_reviewing", "value": "Human Reviewing"},
                        ],
                    },
                    {
                        "id": "sg_stage",
                        "name": "Stage",
                        "options": [
                            {"id": "sg_stage_in_progress", "value": "In Progress"},
                            {"id": "sg_stage_review", "value": "QA / Review"},
                        ],
                    },
                    {
                        "id": "sg_human_reviewer",
                        "name": "Human Reviewer",
                        "options": [{"id": "romman", "value": "Romman"}],
                    },
                ],
            }
        if method == "GET" and path.endswith("/api/v4/users/username/romman"):
            return 200, {"id": self.reviewer_id, "username": "romman"}
        if method == "GET" and path.endswith(f"/boards/{BOARD}/blocks"):
            return 200, [self.card, *self.comments]
        if method == "POST" and path == "/api/v4/posts":
            self.posts.append(data)
            return 201, {"id": "p" * 26}
        if method == "POST" and path.endswith(f"/boards/{BOARD}/blocks"):
            self.comments.extend(data)
            return 201, data
        if method == "PATCH" and path.endswith(f"/boards/{BOARD}/blocks/{CARD}"):
            self.patches.append(data)
            if self.fail_patch_once:
                self.fail_patch_once = False
                return 500, {"message": "retry"}
            if self.ignore_patch_once:
                self.ignore_patch_once = False
                return 200, self.card
            updated = data["updatedFields"]
            fields = dict(self.card.get("fields") or {})
            fields.update(updated)
            self.card = {**self.card, "fields": fields}
            return 200, data
        return 404, {"path": path}


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.relay = FakeRelay(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name: str, record) -> Path:
        path = self.relay.outbox / name
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_recreated_result_is_deduped(self):
        first = self.relay.process_file(self.write("result.json", ship_record()))
        second = self.relay.process_file(self.write("result.json", ship_record()))
        self.assertEqual(first["status"], "done")
        self.assertEqual(second["status"], "deduped")
        self.assertEqual(len(self.relay.posts), 1)
        self.assertEqual(self.relay.posts[0]["pending_post_id"], f"firstmate:{first['delivery_key']}")
        self.assertEqual(len(self.relay.comments), 1)

    def test_scout_is_skipped_without_fake_pr(self):
        record = ship_record(mode="scout")
        del record["pr_url"]
        result = self.relay.process_file(self.write("scout.json", record))
        self.assertEqual(result["status"], "skipped-scout")
        self.assertEqual(self.relay.posts, [])
        self.assertEqual(self.relay.patches, [])

    def test_posted_result_resumes_card_sync_without_repost(self):
        self.relay.fail_patch_once = True
        first = self.relay.process_file(self.write("retry.json", ship_record()))
        self.assertEqual(first["status"], "error")
        self.assertEqual(len(self.relay.posts), 1)
        second = self.relay.process_file(self.write("retry.json", ship_record()))
        self.assertEqual(second["status"], "done")
        self.assertEqual(len(self.relay.posts), 1)
        self.assertEqual(len(self.relay.comments), 1)

    def test_bad_board_and_unknown_status_preflight_before_post(self):
        bad = ship_record(board_id="Lead Ops App")
        result = self.relay.process_file(self.write("bad-board.json", bad))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.relay.posts, [])

        unknown = ship_record(new_status="Mystery")
        result = self.relay.process_file(self.write("bad-status.json", unknown))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.relay.posts, [])

    def test_missing_board_and_card_preflight_before_post(self):
        self.relay.board_ok = False
        result = self.relay.process_file(self.write("missing-board.json", ship_record()))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.relay.posts, [])
        self.relay.board_ok = True
        self.relay.card["id"] = "z" * 27
        result = self.relay.process_file(self.write("missing-card.json", ship_record()))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.relay.posts, [])

    def test_root_array_is_rejected_before_post(self):
        result = self.relay.process_file(self.write("array.json", [ship_record()]))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.relay.posts, [])

    def test_full_property_map_preserves_unrelated_fields(self):
        result = self.relay.process_file(self.write("preserve.json", ship_record()))
        self.assertEqual(result["status"], "done")
        payload = self.relay.patches[0]
        properties = payload["updatedFields"]["properties"]
        self.assertEqual(properties["sg_stage"], "sg_stage_review")
        self.assertEqual(properties["sg_status"], "sg_status_human_reviewing")
        self.assertEqual(properties["sg_owner"], self.relay.reviewer_id)
        self.assertEqual(properties["sg_ai_owner"], "kenza")
        self.assertEqual(properties["sg_priority"], "sg_prio_high")
        self.assertEqual(properties["unrelated"], "preserve-me")
        content_order = payload["updatedFields"]["contentOrder"]
        self.assertEqual(content_order[0], "text-1")
        self.assertEqual(content_order[1], self.relay.comments[0]["id"])
        self.assertIn("QA: Romman checks the PR", self.relay.comments[0]["title"])

    def test_http_200_without_persisted_handoff_never_records_done(self):
        self.relay.ignore_patch_once = True
        result = self.relay.process_file(self.write("ignored-patch.json", ship_record()))
        self.assertEqual(result["status"], "error")
        self.assertIn("required handoff state did not persist", result["error"])
        self.assertFalse(self.relay.dispatch_ledger.exists())
        marker = self.relay._load_json(
            self.relay.marker_path(self.relay.delivery_key(ship_record())), {}
        )
        self.assertEqual(marker["status"], "posted")

    def test_agentic_development_name_routes_to_exact_channel(self):
        record = ship_record(target_channel="Agentic Development")
        del record["target_channel_id"]
        result = self.relay.process_file(self.write("agentic.json", record))
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["channel_source"], "policy:agentic-development")
        self.assertEqual(self.relay.posts[0]["channel_id"], CHANNEL)


class FakeTrigger(trigger_module.Trigger):
    def __init__(self, root: Path, injector_state: str = "sent", task_exists: bool = False):
        self.args = argparse.Namespace(
            card_id=CARD,
            repo="firstmate",
            brief_path=str(root / "brief.md"),
            board_id=BOARD,
            mode="ship",
            target_channel="Agentic Development",
            target_channel_id=None,
            new_status="QA / Review",
            lane="test",
            idempotency_key=f"firstmate-bridge:{CARD}",
            max=1,
            gate_command="gate",
            injector="injector",
        )
        self.base = "https://mattermost.test"
        self.token = "amina-token"
        self.state_dir = root / "trigger-state"
        self.kenza_values = {"kenza"}
        (root / "brief.md").write_text("Build the bridge.", encoding="utf-8")
        self.injector_state = injector_state
        self.task_exists_value = task_exists
        self.injector_calls = 0
        self.patches = []
        self.board = {
            "cardProperties": [
                {
                    "id": "sg_status",
                    "name": "Status",
                    "options": [
                        {"id": "sg_status_ready_ai", "value": "Ready for AI"},
                        {"id": "sg_status_ai_working", "value": "AI Working"},
                        {"id": "sg_status_review", "value": "QA / Review"},
                    ],
                }
            ]
        }
        self.card = {
            "id": CARD,
            "type": "card",
            "deleteAt": 0,
            "fields": {
                "properties": {
                    "sg_status": "sg_status_ready_ai",
                    "sg_ai_owner": "kenza",
                    "sg_human_reviewer": "romman",
                    "sg_canonical_thread": "thread-123",
                    "sg_next_action": "Build the approved code change",
                    "sg_blocker": "none",
                    "sg_acceptance_criteria": "Tests and PR",
                    "sg_work_type": "code",
                    "unrelated": "keep",
                }
            },
        }

    def gate(self):
        return None

    def task_exists_for_card(self, card_id):
        return self.task_exists_value

    def fetch_board(self):
        return self.board, [self.card]

    def api(self, method, path, data=None):
        if method == "PATCH" and path.endswith(f"/boards/{BOARD}/blocks/{CARD}"):
            self.patches.append(data)
            updated_props = ((data or {}).get("updatedFields") or {}).get("properties") or {}
            fields = dict(self.card.get("fields") or {})
            props_copy = dict(fields.get("properties") or {})
            props_copy.update(updated_props)
            fields["properties"] = props_copy
            self.card = {**self.card, "fields": fields}
            return 200, self.card
        if method == "GET" and path.endswith(f"/boards/{BOARD}/blocks"):
            return 200, [self.card]
        return 404, {"path": path}

    def call_injector_once(self, card, options):
        self.injector_calls += 1
        return {"state": self.injector_state, "card_id": CARD}


class TriggerTests(unittest.TestCase):
    def test_one_kenza_card_moves_only_after_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            trigger = FakeTrigger(Path(directory))
            result = trigger.execute()
            self.assertEqual(result["state"], "sent")
            self.assertEqual(trigger.injector_calls, 1)
            self.assertEqual(len(trigger.patches), 1)
            patch = trigger.patches[0]
            self.assertIn("updatedFields", patch)
            self.assertNotIn("id", patch)
            self.assertNotIn("type", patch)
            patch_props = patch["updatedFields"]["properties"]
            self.assertEqual(patch_props.get("sg_status"), "sg_status_ai_working")
            self.assertEqual(result["idempotency_key"], f"firstmate-bridge:{CARD}")
            self.assertEqual(result["max"], 1)

    def test_non_sent_result_never_moves_card(self):
        with tempfile.TemporaryDirectory() as directory:
            trigger = FakeTrigger(Path(directory), injector_state="uncertain")
            with self.assertRaises(RuntimeError):
                trigger.execute()
            self.assertEqual(trigger.injector_calls, 1)
            self.assertEqual(trigger.patches, [])

    def test_existing_task_dedupes_before_injector(self):
        with tempfile.TemporaryDirectory() as directory:
            trigger = FakeTrigger(Path(directory), task_exists=True)
            result = trigger.execute()
            self.assertEqual(result["state"], "deduped-existing-task")
            self.assertEqual(trigger.injector_calls, 0)
            self.assertEqual(trigger.patches, [])

    def test_trigger_rejection_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trigger = FakeTrigger(root)
            trigger.card["fields"]["properties"]["sg_ai_owner"] = "layla"
            with self.assertRaisesRegex(RuntimeError, "AI Owner is not Kenza"):
                trigger.execute()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trigger = FakeTrigger(root)
            trigger.card["fields"]["properties"]["sg_work_type"] = "content"
            with self.assertRaisesRegex(RuntimeError, "not explicitly a code"):
                trigger.execute()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trigger = FakeTrigger(root)
            del trigger.card["fields"]["properties"]["sg_human_reviewer"]
            with self.assertRaisesRegex(RuntimeError, "structured envelope is incomplete"):
                trigger.execute()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trigger = FakeTrigger(root)
            trigger.args.max = 2
            with self.assertRaisesRegex(RuntimeError, "requires --max 1"):
                trigger.execute()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trigger = FakeTrigger(root)
            active = json.loads(json.dumps(trigger.card))
            active["id"] = "z" * 27
            active["fields"]["properties"]["sg_status"] = "sg_status_ai_working"
            trigger.fetch_board = lambda: (trigger.board, [trigger.card, active])
            with self.assertRaisesRegex(RuntimeError, "already has an active"):
                trigger.execute()


if __name__ == "__main__":
    unittest.main(verbosity=2)
