#!/usr/bin/env bash
# Strict, card-idempotent Phase A injector for the Firstmate Bridge.
# Usage:
#   fm-bridge-inject.sh --repo NAME --brief-path PATH --card-id ID --mode ship|scout \
#     --canonical-thread VALUE [--target-channel NAME|--target-channel-id ID] \
#     [--board-id ID --new-status LABEL --status-option LABEL ...]
#
# Runtime state lives under $FM_HOME/state/bridge. Creating
# $FM_HOME/state/bridge/PAUSED is the kill switch. The switch is checked before
# a card claim and again while the inter-process lock is held.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FM_ROOT="${FM_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
FM_HOME="${FM_HOME:-${FM_ROOT_OVERRIDE:-$FM_ROOT}}"
STATE_ROOT="${FM_BRIDGE_STATE_DIR:-$FM_HOME/state/bridge}"
DATA_ROOT="${FM_BRIDGE_DATA_DIR:-$FM_HOME/data/bridge}"
PROJECTS_FILE="${FM_BRIDGE_PROJECTS_FILE:-$FM_HOME/data/projects.md}"
KILL_SWITCH="${FM_BRIDGE_KILL_SWITCH:-$STATE_ROOT/PAUSED}"
LEDGER="${FM_BRIDGE_LEDGER:-$STATE_ROOT/dispatch-ledger.jsonl}"
LOG="${FM_BRIDGE_LOG:-$STATE_ROOT/injector.jsonl}"
LOCK="${FM_BRIDGE_LOCK:-$STATE_ROOT/injector.lock}"
LIVE_LIMIT=10

# shellcheck source=bin/fm-tmux-lib.sh
. "$SCRIPT_DIR/fm-tmux-lib.sh"

usage() {
  sed -n '2,7p' "$0" >&2
}

repo=
brief_path=
card_id=
mode=
canonical_thread=
target_channel=
target_channel_id=
board_id=
new_status=
status_options=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) shift; repo=${1:-} ;;
    --brief-path) shift; brief_path=${1:-} ;;
    --card-id) shift; card_id=${1:-} ;;
    --mode) shift; mode=${1:-} ;;
    --canonical-thread) shift; canonical_thread=${1:-} ;;
    --target-channel) shift; target_channel=${1:-} ;;
    --target-channel-id) shift; target_channel_id=${1:-} ;;
    --board-id) shift; board_id=${1:-} ;;
    --new-status) shift; new_status=${1:-} ;;
    --status-option) shift; status_options+=("${1:-}") ;;
    --help|-h) usage; exit 0 ;;
    *) echo "fm-bridge-inject: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
  shift || true
done

emit_result() {
  local state=$1 reason=$2 target=${3:-}
  jq -cn --arg state "$state" --arg reason "$reason" --arg card_id "$card_id" \
    --arg repo "$repo" --arg mode "$mode" --arg target "$target" \
    '{state:$state,reason:$reason,card_id:$card_id,repo:$repo,mode:$mode,target:$target}'
}

single_line() {
  [ -n "$1" ] && ! printf '%s' "$1" | LC_ALL=C grep -q '[[:cntrl:]]'
}

for required in repo brief_path card_id mode canonical_thread; do
  value=${!required}
  single_line "$value" || {
    echo "fm-bridge-inject: $required is required and must contain no control characters" >&2
    exit 2
  }
done
case "$mode" in ship|scout) ;; *) echo "fm-bridge-inject: mode must be ship or scout" >&2; exit 2 ;; esac
case "$repo" in firstmate|bracket_report|lead-ops-agent) ;; *)
  echo "fm-bridge-inject: repo is not in the exact bridge allowlist" >&2
  exit 2
  ;;
esac
printf '%s' "$card_id" | grep -Eq '^[a-z0-9]{27}$' || {
  echo "fm-bridge-inject: card_id must be a 27-character Focalboard id" >&2
  exit 2
}
[ -r "$brief_path" ] || { echo "fm-bridge-inject: brief is not readable: $brief_path" >&2; exit 2; }
[ -f "$PROJECTS_FILE" ] || { echo "fm-bridge-inject: project registry is unavailable" >&2; exit 2; }
awk -v name="$repo" '$1=="-" && $2==name { found=1 } END { exit !found }' "$PROJECTS_FILE" || {
  echo "fm-bridge-inject: repo is not exactly registered in $PROJECTS_FILE" >&2
  exit 2
}
[ -d "$FM_HOME/projects/$repo" ] || {
  echo "fm-bridge-inject: registered project checkout is absent: $repo" >&2
  exit 2
}
if [ -n "$target_channel" ]; then single_line "$target_channel" || exit 2; fi
if [ -n "$target_channel_id" ]; then
  printf '%s' "$target_channel_id" | grep -Eq '^[a-z0-9]{26}$' || exit 2
fi
if [ -n "$board_id" ] || [ -n "$new_status" ]; then
  printf '%s' "$board_id" | grep -Eq '^[a-z0-9]{27}$' || {
    echo "fm-bridge-inject: board_id must be a 27-character Focalboard id" >&2; exit 2; }
  single_line "$new_status" || { echo "fm-bridge-inject: new_status is required with board_id" >&2; exit 2; }
  [ "${#status_options[@]}" -gt 0 ] || {
    echo "fm-bridge-inject: board sync requires live --status-option labels" >&2; exit 2; }
fi
for option in "${status_options[@]}"; do
  single_line "$option" || { echo "fm-bridge-inject: status options must be single-line labels" >&2; exit 2; }
done

# Fail safe on protected or operational intent. Negated policy statements such
# as "never deploy" are allowed; an affirmative occurrence is refused.
if ! python3 - "$brief_path" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read().lower()
patterns = [
    r"\bdeploy(?:ment|s|ed|ing)?\b", r"\bmerge(?:s|d|ing)?\b",
    r"\bprod(?:uction)?\b", r"\bmain\b", r"\bdns\b",
    r"\bsecret(?:s)?\b", r"\bcredential(?:s)?\b",
    r"\b(?:delete|destroy|wipe|purge|drop|truncate)(?:s|d|ing)?\b",
    r"\bforce[- ]?push\b", r"\breset\s+--hard\b",
]
negated = re.compile(r"(?:(?:\bnever\b|\bdo not\b|\bdon't\b|\bmust not\b|\bno\b).{0,48}|\bwithout\b.{0,16})$")
for pattern in patterns:
    for match in re.finditer(pattern, text):
        if not negated.search(text[max(0, match.start()-64):match.start()]):
            print(f"protected intent matched: {match.group(0)}", file=sys.stderr)
            raise SystemExit(1)
PY
then
  echo "fm-bridge-inject: protected, production, secret, DNS, merge, deploy, or destructive intent refused" >&2
  exit 2
fi

[ ! -e "$KILL_SWITCH" ] || {
  emit_result "failed-before-send" "bridge kill switch is active"
  exit 1
}

mkdir -p "$STATE_ROOT" "$DATA_ROOT/dispatch" || {
  echo "fm-bridge-inject: cannot create bridge state" >&2; exit 1; }
exec 9<>"$LOCK"
flock -w 5 9 || { emit_result "failed-before-send" "injector lock is busy"; exit 1; }
[ ! -e "$KILL_SWITCH" ] || {
  emit_result "failed-before-send" "bridge kill switch is active"
  exit 1
}

latest_state=
if [ -f "$LEDGER" ]; then
  latest_state=$(jq -rs --arg card "$card_id" '[.[] | select(.card_id == $card)] | last | .state // ""' "$LEDGER" 2>/dev/null || true)
fi
case "$latest_state" in
  claimed|sent|uncertain|completed)
    emit_result "$latest_state" "card already has a durable bridge claim"
    exit 0
    ;;
esac

active_bridge=0
if [ -f "$LEDGER" ]; then
  active_bridge=$(jq -rs '
    group_by(.card_id)
    | map(last)
    | map(select(.state == "claimed" or .state == "sent" or .state == "uncertain"))
    | length
  ' "$LEDGER" 2>/dev/null || printf '1')
fi
[ "$active_bridge" -lt 1 ] || {
  emit_result "failed-before-send" "Phase A already has one active bridge card"
  exit 1
}

live_tasks=0
if [ -d "$FM_HOME/state" ]; then
  shopt -s nullglob
  for meta in "$FM_HOME"/state/*.meta; do
    grep -q '^kind=secondmate$' "$meta" 2>/dev/null && continue
    window=$(awk -F= '$1=="window" {v=substr($0,index($0,"=")+1)} END {print v}' "$meta")
    if [ -n "$window" ] && tmux display-message -p -t "$window" '#{pane_id}' >/dev/null 2>&1; then
      live_tasks=$((live_tasks + 1))
      continue
    fi
    task_name=${meta##*/}
    task_name=${task_name%.meta}
    current=$(FM_CREW_STATE_NM_TIMEOUT="${FM_BRIDGE_CREW_STATE_TIMEOUT:-2}" \
      "$SCRIPT_DIR/fm-crew-state.sh" "$task_name" 2>/dev/null || true)
    case "$current" in
      'state: working'*|'state: parked'*|'state: blocked'*) live_tasks=$((live_tasks + 1)) ;;
    esac
  done
  shopt -u nullglob
fi
[ "$live_tasks" -lt "$LIVE_LIMIT" ] || {
  emit_result "failed-before-send" "authoritative live Firstmate task cap of 10 reached"
  exit 1
}

digest=$(sha256sum "$brief_path" | awk '{print $1}')
task_id="bridge-${card_id:0:12}"
append_ledger() {
  local state=$1 transport=$2 retry=$3 target=${4:-} reason=${5:-}
  jq -cn --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg state "$state" \
    --arg card_id "$card_id" --arg repo "$repo" --arg mode "$mode" \
    --arg target "$target" --arg digest "$digest" --arg transport "$transport" \
    --arg retry_decision "$retry" --arg reason "$reason" \
    '{at:$at,state:$state,card_id:$card_id,repo:$repo,mode:$mode,target:$target,digest:$digest,transport:$transport,retry_decision:$retry_decision,reason:$reason}' \
    | tee -a "$LEDGER" >> "$LOG"
}
append_ledger claimed not-started no-retry "" "atomic card claim"

fail_after_claim() {
  local reason=$1
  append_ledger failed-before-send not-started retry-safe-no-send "" "$reason"
  emit_result failed-before-send "$reason"
  exit 1
}

tmux has-session -t fm 2>/dev/null || fail_after_claim "tmux session fm is absent"

command -v pstree >/dev/null 2>&1 || fail_after_claim "pstree is required but not installed (Ubuntu/WSL2: sudo apt-get install psmisc)"

process_has_codex() {
  local pid=$1
  pstree -ap "$pid" 2>/dev/null | grep -Eq '(^|[ ,/])codex([ ,)]|$)|node[^,]*,/usr/bin/codex'
}

captain_candidates=()
stable_candidates=()
while IFS=$'\t' read -r target window_name pane_path pane_pid; do
  [ "$pane_path" = "$FM_HOME" ] || continue
  process_has_codex "$pane_pid" || continue
  captain_candidates+=("$target")
  [ "$window_name" = captain ] && stable_candidates+=("$target")
done < <(tmux list-panes -s -t fm -F '#{session_name}:#{window_name}.#{pane_index}\t#{window_name}\t#{pane_current_path}\t#{pane_pid}' 2>/dev/null)

target=
if [ "${#stable_candidates[@]}" -eq 1 ]; then
  target=${stable_candidates[0]}
elif [ "${#stable_candidates[@]}" -gt 1 ]; then
  fail_after_claim "stable captain window is ambiguous"
elif [ "${#captain_candidates[@]}" -eq 1 ]; then
  target=${captain_candidates[0]}
else
  fail_after_claim "captain pane cannot be uniquely verified"
fi

composer=$(fm_tmux_composer_state "$target")
[ "$composer" = empty ] || fail_after_claim "captain composer is $composer"
if fm_pane_is_busy "$target"; then
  fail_after_claim "captain pane is busy"
fi

metadata="$DATA_ROOT/dispatch/$card_id.json"
tmp_metadata="$metadata.tmp.$$"
jq_args=(
  -n --arg schema_version "1.0" --arg record_type dispatch --arg task_id "$task_id"
  --arg card_id "$card_id" --arg repo "$repo" --arg mode "$mode"
  --arg brief_path "$brief_path" --arg canonical_thread "$canonical_thread"
  --arg target_channel "$target_channel" --arg target_channel_id "$target_channel_id"
  --arg board_id "$board_id" --arg new_status "$new_status"
)
# shellcheck disable=SC2016 # jq variables are intentionally expanded by jq.
jq_filter='{
  schema_version:$schema_version,record_type:$record_type,task_id:$task_id,
  card_id:$card_id,repo:$repo,mode:$mode,brief_path:$brief_path,
  canonical_thread:$canonical_thread
}
| if $target_channel != "" then .target_channel=$target_channel else . end
| if $target_channel_id != "" then .target_channel_id=$target_channel_id else . end
| if $board_id != "" then .board_id=$board_id | .new_status=$new_status else . end'
jq "${jq_args[@]}" "$jq_filter" > "$tmp_metadata" || fail_after_claim "cannot write dispatch metadata"
validator_args=("$SCRIPT_DIR/fm-outbox-validate.py" "$tmp_metadata")
for option in "${status_options[@]}"; do validator_args+=(--status-option "$option"); done
python3 "${validator_args[@]}" >/dev/null || fail_after_claim "dispatch metadata failed contract validation"
mv "$tmp_metadata" "$metadata" || fail_after_claim "cannot publish dispatch metadata"

line="ahoy $mode: read and execute $brief_path; repo=$repo; card=$card_id; contract=$metadata"
single_line "$line" || fail_after_claim "canonical dispatch line contains control characters"

set +e
transport_out=$(FM_SEND_SETTLE="${FM_BRIDGE_SEND_SETTLE:-1}" "$SCRIPT_DIR/fm-send.sh" --strict-ack "$target" "$line")
transport_rc=$?
set -e
case "$transport_rc:$transport_out" in
  0:sent)
    append_ledger sent sent never "$target" "strict acknowledgement received"
    emit_result sent "strict acknowledgement received" "$target"
    exit 0
    ;;
  2:failed-before-send)
    append_ledger failed-before-send failed-before-send retry-safe-no-send "$target" "transport rejected before literal send"
    emit_result failed-before-send "transport rejected before literal send" "$target"
    exit 1
    ;;
  *)
    append_ledger uncertain uncertain never-auto-retype "$target" "submission acknowledgement is uncertain"
    emit_result uncertain "submission acknowledgement is uncertain; never auto-retype" "$target"
    exit 3
    ;;
esac
