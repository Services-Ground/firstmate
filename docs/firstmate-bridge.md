# Firstmate Bridge Phase A

Phase A provides a programmatic code-task handoff into a running Firstmate captain and a hardened return relay.
It is Kenza-triggered and processes one bridge card at a time.
It never merges, deploys, changes production, or makes protected infrastructure changes.

## Tracked source of truth

- `docs/contracts/firstmate-outbox-v1.schema.json` is the versioned root-object contract.
- `bin/fm_outbox_contract.py` is the dependency-free runtime validator.
- `bin/fm-outbox-validate.py` is its command-line entry point.
- `bin/fm-bridge-inject.sh` is the strict card-idempotent injector.
- `bin/sg-firstmate-kenza-trigger.py` is the manual Department Driver adapter for one Kenza code card.
- `bin/sg-firstmate-relay.py` is the canonical deployable Hermes relay source.

The live Hermes script is not the source of truth.
Do not hand-edit `/home/hp/.hermes/scripts/sg_firstmate_relay.py`.
The tracked relay is copied into place only during the Abdul-gated apply.

## Contract

Every record is one JSON root object with `schema_version: "1.0"`.
Arrays, aliases, and unknown properties are rejected.
Dispatch metadata uses `record_type: "dispatch"` and includes the readable brief path plus canonical thread.
Outbox files use `record_type: "result"`.

A ship result requires:

```json
{
  "schema_version": "1.0",
  "record_type": "result",
  "task_id": "firstmate-bridge-build",
  "card_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "repo": "firstmate",
  "mode": "ship",
  "pr_url": "https://github.com/Services-Ground/firstmate/pull/123",
  "risk": "medium",
  "summary": "Phase A bridge implementation and tests.",
  "target_channel": "Agentic Development",
  "board_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "new_status": "QA / Review",
  "abdul_gated_apply": [
    "Mask the legacy firstmate-mattermost-outbox.path unit and verify both watcher units inactive and masked.",
    "Provide captain runtime persistence and stable fm:captain identity before Phase B."
  ]
}
```

`target_channel_id` is preferred when known.
`target_channel` is used only when it resolves exactly, while an absent target falls back to the policy's Agentic Development channel.
Focalboard board and card IDs are 27 characters; Mattermost channel IDs are 26 characters.
`board_id` and `new_status` must appear together.
The validator requires the exact live board option labels whenever a board sync is requested.
A scout result is explicit with `mode: "scout"`, contains no `pr_url`, and is archived without a fake PR post.

Validate a record against live option labels before placing it in the outbox:

```sh
bin/fm-outbox-validate.py result.json \
  --status-option 'Ready for AI' \
  --status-option 'AI Working' \
  --status-option 'QA / Review'
```

## Injector safety

The injector accepts one exact registered repository, readable brief, opaque card id, `ship|scout` mode, and canonical routing metadata.
The allowed registered repository names are `firstmate`, `bracket_report`, and `lead-ops-agent`.
Unknown names are refused and never fall back to `firstmate`.

Create `$FM_HOME/state/bridge/PAUSED` to activate the kill switch.
The injector checks it before taking a card claim and again while holding its inter-process lock.
Claims and delivery outcomes are appended to `$FM_HOME/state/bridge/dispatch-ledger.jsonl` with `claimed`, `sent`, `uncertain`, `failed-before-send`, and `completed` states.
An uncertain result is never automatically retyped.

Before the claim is taken, the brief is scanned for affirmative protected-intent patterns: deploy, merge, prod/production, main, dns, secrets/credentials, destructive operations (delete, destroy, wipe, purge, drop, truncate), force-push, and hard reset.
A negated context (for example "never deploy" or "do not merge") is allowed; an affirmative occurrence exits with `failed-before-send` before any claim is recorded in the ledger.

The injector counts live task metadata whose recorded tmux target still exists and refuses the eleventh task.
It also permits only one active bridge card in Phase A.
It requires a unique Codex captain pane in session `fm`, preferring a verified `fm:captain` window and otherwise requiring the unique pane rooted at `$FM_HOME` with a Codex descendant.
It refuses busy, unreadable, ambiguous, and non-empty composers before sending one canonical line through `fm-send.sh --strict-ack`.

## Kenza trigger

`sg-firstmate-kenza-trigger.py` is manually invoked with one canonical card id.
It is not a scanner or scheduler.
It runs the Department Driver gate, requires a complete structured envelope, `Ready for AI`, AI Owner Kenza, an explicit code work type, a reviewer, and no other Kenza `AI Working` card.
It reads Hermes Kanban state for `task_exists_for_card` dedupe, fixes the idempotency key to `firstmate-bridge:<card_id>`, and enforces `--max 1`.
It calls the injector once and moves the card to `AI Working` only after the injector returns durable `sent`.
The Focalboard patch carries the full existing property map.

## Relay behavior

The relay takes an inter-process lock and validates the contract before any Mattermost post.
It posts to Mattermost as Ron and patches Focalboard as Amina; each identity loads its token from the Hermes profile environment independently.
For card-bound results it fetches the board and card and resolves the exact live status option before posting.
Bad boards, missing cards, and unknown status labels therefore cannot leave a posted-but-unsynced handoff.

A stable hash of schema version, task, card, and PR is the delivery key.
The relay stores a durable two-phase marker after Mattermost posts and uses the same key as `pending_post_id`.
Retries resume card sync without reposting, and recreating a result filename is deduped even after the original moved to `done/`.
Focalboard status writes copy the full current `fields.properties` map and change only the status property.
Successful ship delivery appends `completed` to the injector ledger so the next Phase A card can run.

## Abdul-gated apply

No build or test step installs or restarts live services.
The apply owner must separately review and perform these remaining changes:

1. Deploy the tracked relay and trigger source into the approved Hermes paths, retaining local environment secrets outside git.
2. Keep the current relay cron only after verifying the deployed checksum and a dry-run fixture.
3. Mask `firstmate-mattermost-outbox.path` as well as the already masked service, then verify both legacy units are inactive and masked.
4. Before Phase B, provide restart-on-reboot captain runtime persistence and a stable `fm:captain` identity.

Phase A fails closed while the captain is absent.
Phase B auto-selection remains out of scope until the runtime prerequisite is approved and applied.
