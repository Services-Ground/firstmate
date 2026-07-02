#!/usr/bin/env bash
# Scan firstmate's local data/outbox/*.json records and post new PR entries to
# Mattermost through hermes, optionally syncing the result to a Focalboard card.
#
# Default mode is one scan, suitable for systemd path activation:
#   bin/fm-mattermost-outbox-watch.sh
#
# Optional polling mode is useful for manual smoke tests:
#   bin/fm-mattermost-outbox-watch.sh --watch
#
# Config:
#   FM_MATTERMOST_TARGET       Direct hermes target, e.g. mattermost:<post>:<thread>.
#   FM_MATTERMOST_THREAD_NAME  Target label to resolve from `hermes send --list
#                              mattermost --json`; defaults to SG AI Coordination.
#   FM_MATTERMOST_OUTBOX_DIR   Override input dir; defaults to $FM_HOME/data/outbox.
#   FM_MATTERMOST_STATE_DIR    Override durable state dir; defaults to
#                              $FM_HOME/state/mattermost-outbox.
#   FM_MATTERMOST_POLL         Poll interval for --watch; defaults to 5 seconds.
#   FM_FOCALBOARD_URL          Focalboard API base URL.
#   FM_FOCALBOARD_TOKEN        Focalboard bearer token.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FM_ROOT="${FM_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
FM_HOME="${FM_HOME:-${FM_ROOT_OVERRIDE:-$FM_ROOT}}"
OUTBOX="${FM_MATTERMOST_OUTBOX_DIR:-$FM_HOME/data/outbox}"
STATE="${FM_MATTERMOST_STATE_DIR:-$FM_HOME/state/mattermost-outbox}"
THREAD_NAME="${FM_MATTERMOST_THREAD_NAME:-SG AI Coordination}"
POLL="${FM_MATTERMOST_POLL:-5}"
DEFAULT_TARGET=

LOCK_FD=
trap '[ -n "${LOCK_FD:-}" ] && eval "exec ${LOCK_FD}>&-" 2>/dev/null || true' EXIT

usage() {
  cat >&2 <<'EOF'
usage: fm-mattermost-outbox-watch.sh [--once|--watch]

Scan data/outbox/*.json for new PR entries and post each PR URL plus risk to
Mattermost through hermes. Entries may name a target_channel_id and Focalboard
card fields; older entries still fall back to SG AI Coordination.
EOF
}

MODE=once
case "${1:-}" in
  ""|--once) MODE=once ;;
  --watch) MODE=watch ;;
  --help|-h) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac

case "$POLL" in
  ''|*[!0-9]*) echo "fm-mattermost-outbox-watch: invalid FM_MATTERMOST_POLL: $POLL" >&2; exit 2 ;;
esac

mkdir_p_state() {
  mkdir -p "$STATE/posted" "$STATE/focalboard/commented" "$STATE/focalboard/moved" 2>/dev/null || {
    echo "fm-mattermost-outbox-watch: cannot create state dir: $STATE" >&2
    return 1
  }
}

with_lock() {
  mkdir_p_state || return 1
  exec {LOCK_FD}<>"$STATE/lock.flock"
  if ! flock -w 2 "$LOCK_FD"; then
    echo "fm-mattermost-outbox-watch: another scan is still running" >&2
    return 1
  fi
  scan_once; local rc=$?
  flock -u "$LOCK_FD"
  eval "exec ${LOCK_FD}>&-"
  LOCK_FD=
  return "$rc"
}

hermes_target() {
  if [ -n "${FM_MATTERMOST_TARGET:-}" ]; then
    printf '%s\n' "$FM_MATTERMOST_TARGET"
    return 0
  fi

  command -v hermes >/dev/null 2>&1 || {
    echo "fm-mattermost-outbox-watch: hermes not found; set PATH or install hermes" >&2
    return 1
  }
  command -v jq >/dev/null 2>&1 || {
    echo "fm-mattermost-outbox-watch: jq not found" >&2
    return 1
  }

  local targets target_id
  targets=$(hermes send --list mattermost --json 2>/dev/null) || {
    echo "fm-mattermost-outbox-watch: cannot list hermes Mattermost targets" >&2
    return 1
  }
  target_id=$(printf '%s\n' "$targets" | jq -r --arg name "$THREAD_NAME" '
    [.platforms.mattermost[]? | select(.name == $name) | .id] as $exact
    | if ($exact | length) == 1 then $exact[0]
      elif ($exact | length) > 1 then "AMBIGUOUS"
      else
        [.platforms.mattermost[]? | select(.name | contains($name)) | .id] as $partial
        | if ($partial | length) == 1 then $partial[0]
          elif ($partial | length) > 1 then "AMBIGUOUS"
          else empty end
      end
  ' 2>/dev/null) || target_id=

  case "$target_id" in
    "")
      echo "fm-mattermost-outbox-watch: cannot resolve Mattermost target '$THREAD_NAME'; set FM_MATTERMOST_TARGET" >&2
      return 1
      ;;
    AMBIGUOUS)
      echo "fm-mattermost-outbox-watch: Mattermost target '$THREAD_NAME' is ambiguous; set FM_MATTERMOST_TARGET" >&2
      return 1
      ;;
  esac
  printf 'mattermost:%s\n' "$target_id"
}

json_pr_url() {
  local file=$1
  jq -r '
    [
      .pr_url?,
      .pull_request_url?,
      .html_url?,
      .url?,
      .pr.url?,
      .pr.html_url?,
      .pull_request.url?,
      .pull_request.html_url?
    ]
    | map(select(type == "string" and test("^https?://github[.]com/[^[:space:]]+/pull/[0-9]+([/?#].*)?$")))
    | .[0] // empty
  ' "$file"
}

json_risk() {
  local file=$1
  jq -r '
    [
      .risk?,
      .risk_value?,
      .emitted_risk?,
      .validation.risk?,
      .checks.risk?
    ]
    | map(select(. != null))
    | .[0] // "unknown"
    | if type == "string" then . else tostring end
  ' "$file"
}

json_summary() {
  local file=$1
  jq -r '
    [
      .summary?,
      .message?,
      .description?
    ]
    | map(select(. != null))
    | .[0] // ""
    | if type == "string" then . else tostring end
  ' "$file"
}

json_string_field() {
  local file=$1 field=$2
  jq -r --arg field "$field" '
    .[$field]? // ""
    | if type == "string" then . else tostring end
  ' "$file"
}

post_key() {
  printf '%s' "$1" | sha256sum | awk '{print $1}'
}

mattermost_target_for() {
  local file=$1 channel_id
  channel_id=$(json_string_field "$file" target_channel_id 2>/dev/null) || return 1
  if [ -n "$channel_id" ]; then
    printf 'mattermost:%s\n' "$channel_id"
    return 0
  fi
  if [ -z "$DEFAULT_TARGET" ]; then
    DEFAULT_TARGET=$(hermes_target) || return 1
  fi
  printf '%s\n' "$DEFAULT_TARGET"
}

mattermost_marker_for() {
  local url=$1 target=$2 channel_id=$3 key
  if [ -n "$channel_id" ]; then
    key=$(post_key "$url|mattermost|$target")
  else
    key=$(post_key "$url")
  fi
  printf '%s/posted/%s.posted\n' "$STATE" "$key"
}

message_file_for() {
  local url=$1 risk=$2 summary=$3 out=$4
  {
    [ -n "$summary" ] && printf 'Summary: %s\n' "$summary"
    printf 'PR: %s\n' "$url"
    printf 'Risk: %s\n' "$risk"
  } > "$out"
}

record_marker() {
  local marker=$1 tmp
  shift
  tmp="$marker.tmp.$$"
  {
    while [ "$#" -gt 0 ]; do
      printf '%s\n' "$1"
      shift
    done
    date -u '+posted_at=%Y-%m-%dT%H:%M:%SZ'
  } > "$tmp" && mv -f "$tmp" "$marker"
}

post_mattermost() {
  local file=$1 url=$2 risk=$3 summary=$4 target channel_id marker msg rc
  target=$(mattermost_target_for "$file") || return 1
  channel_id=$(json_string_field "$file" target_channel_id 2>/dev/null) || channel_id=
  marker=$(mattermost_marker_for "$url" "$target" "$channel_id")
  [ -e "$marker" ] && return 0

  msg=$(mktemp "${TMPDIR:-/tmp}/fm-mattermost-outbox.XXXXXX") || {
    echo "fm-mattermost-outbox-watch: cannot create temp message" >&2
    return 1
  }
  message_file_for "$url" "$risk" "$summary" "$msg"
  hermes send --to "$target" --file "$msg" --quiet
  rc=$?
  rm -f "$msg"
  if [ "$rc" -ne 0 ]; then
    echo "fm-mattermost-outbox-watch: hermes send failed for $file" >&2
    return 1
  fi

  record_marker "$marker" \
    "url=$url" \
    "risk=$risk" \
    "target=$target" \
    "source=$file" || return 1
}

focalboard_api_base() {
  local base=${FM_FOCALBOARD_URL:-}
  base=${base%/}
  printf '%s\n' "$base"
}

focalboard_comment_text() {
  local url=$1 risk=$2 summary=$3
  jq -rn --arg summary "$summary" --arg url "$url" --arg risk "$risk" '
    [
      (if $summary != "" then "Summary: \($summary)" else empty end),
      "PR: \($url)",
      "Risk: \($risk)"
    ] | join("\n")
  '
}

focalboard_comment_payload() {
  local board_id=$1 card_id=$2 url=$3 risk=$4 summary=$5 key now title
  key=$(post_key "$url|focalboard-comment|$board_id|$card_id")
  now=$(date '+%s%3N')
  title=$(focalboard_comment_text "$url" "$risk" "$summary") || return 1
  jq -cn \
    --arg id "fm-$key" \
    --arg board_id "$board_id" \
    --arg card_id "$card_id" \
    --argjson now "$now" \
    --arg title "$title" \
    '[{
      id: $id[0:26],
      parentId: $card_id,
      schema: 1,
      type: "comment",
      title: $title,
      fields: {},
      createAt: $now,
      updateAt: $now,
      boardId: $board_id
    }]'
}

focalboard_status_payload() {
  local new_status=$1
  jq -cn --arg new_status "$new_status" '{updatedProperties:{status:$new_status}}'
}

focalboard_curl() {
  local method=$1 url=$2 data=$3
  curl -fsS -X "$method" \
    -H "Authorization: Bearer $FM_FOCALBOARD_TOKEN" \
    -H "Content-Type: application/json" \
    -H "X-Requested-With: XMLHttpRequest" \
    --data "$data" \
    "$url" >/dev/null
}

sync_focalboard() {
  local file=$1 url=$2 risk=$3 summary=$4 board_id card_id new_status base marker payload key
  board_id=$(json_string_field "$file" board_id 2>/dev/null) || return 1
  card_id=$(json_string_field "$file" card_id 2>/dev/null) || return 1
  new_status=$(json_string_field "$file" new_status 2>/dev/null) || return 1
  [ -n "$board_id" ] && [ -n "$card_id" ] || return 0

  if [ -z "${FM_FOCALBOARD_URL:-}" ] || [ -z "${FM_FOCALBOARD_TOKEN:-}" ]; then
    echo "fm-mattermost-outbox-watch: warning: Focalboard card sync requested for $file but FM_FOCALBOARD_URL or FM_FOCALBOARD_TOKEN is missing; Mattermost posting was still attempted" >&2
    return 0
  fi
  command -v curl >/dev/null 2>&1 || {
    echo "fm-mattermost-outbox-watch: curl not found for Focalboard sync" >&2
    return 1
  }

  base=$(focalboard_api_base)
  key=$(post_key "$url|focalboard-comment|$board_id|$card_id")
  marker="$STATE/focalboard/commented/$key.posted"
  if [ ! -e "$marker" ]; then
    payload=$(focalboard_comment_payload "$board_id" "$card_id" "$url" "$risk" "$summary") || return 1
    focalboard_curl POST "$base/boards/$board_id/blocks?disable_notify=true" "$payload" || {
      echo "fm-mattermost-outbox-watch: Focalboard comment failed for $file" >&2
      return 1
    }
    record_marker "$marker" \
      "url=$url" \
      "board_id=$board_id" \
      "card_id=$card_id" \
      "source=$file" || return 1
  fi

  [ -n "$new_status" ] || return 0
  key=$(post_key "$url|focalboard-status|$board_id|$card_id|$new_status")
  marker="$STATE/focalboard/moved/$key.posted"
  [ -e "$marker" ] && return 0
  payload=$(focalboard_status_payload "$new_status") || return 1
  focalboard_curl PATCH "$base/cards/$card_id?disable_notify=true" "$payload" || {
    echo "fm-mattermost-outbox-watch: Focalboard status move failed for $file" >&2
    return 1
  }
  record_marker "$marker" \
    "url=$url" \
    "board_id=$board_id" \
    "card_id=$card_id" \
    "new_status=$new_status" \
    "source=$file" || return 1
}

post_pr() {
  local file=$1 url risk summary
  url=$(json_pr_url "$file" 2>/dev/null) || {
    echo "fm-mattermost-outbox-watch: malformed JSON: $file" >&2
    return 0
  }
  [ -n "$url" ] || return 0

  risk=$(json_risk "$file" 2>/dev/null) || risk=unknown
  summary=$(json_summary "$file" 2>/dev/null) || summary=
  post_mattermost "$file" "$url" "$risk" "$summary" || return 1
  sync_focalboard "$file" "$url" "$risk" "$summary" || return 1
}

scan_once() {
  [ -d "$OUTBOX" ] || return 0
  command -v hermes >/dev/null 2>&1 || {
    echo "fm-mattermost-outbox-watch: hermes not found; set PATH or install hermes" >&2
    return 1
  }
  command -v jq >/dev/null 2>&1 || {
    echo "fm-mattermost-outbox-watch: jq not found" >&2
    return 1
  }

  local file
  find "$OUTBOX" -maxdepth 1 -type f -name '*.json' -print | LC_ALL=C sort | while IFS= read -r file; do
    post_pr "$file" || exit 1
  done
}

if [ "$MODE" = watch ]; then
  while :; do
    with_lock || exit 1
    sleep "$POLL"
  done
else
  with_lock
fi
