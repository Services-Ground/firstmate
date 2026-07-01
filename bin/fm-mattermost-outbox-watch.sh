#!/usr/bin/env bash
# Scan firstmate's local data/outbox/*.json records and post new PR entries to
# the canonical Services Ground Mattermost coordination thread through hermes.
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
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FM_ROOT="${FM_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
FM_HOME="${FM_HOME:-${FM_ROOT_OVERRIDE:-$FM_ROOT}}"
OUTBOX="${FM_MATTERMOST_OUTBOX_DIR:-$FM_HOME/data/outbox}"
STATE="${FM_MATTERMOST_STATE_DIR:-$FM_HOME/state/mattermost-outbox}"
THREAD_NAME="${FM_MATTERMOST_THREAD_NAME:-SG AI Coordination}"
POLL="${FM_MATTERMOST_POLL:-5}"

_LOCK_PATH=
trap 'rmdir "$_LOCK_PATH" 2>/dev/null || true' EXIT

usage() {
  cat >&2 <<'EOF'
usage: fm-mattermost-outbox-watch.sh [--once|--watch]

Scan data/outbox/*.json for new PR entries and post each PR URL plus risk to the
Services Ground Mattermost coordination thread through hermes.
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
  mkdir -p "$STATE/posted" 2>/dev/null || {
    echo "fm-mattermost-outbox-watch: cannot create state dir: $STATE" >&2
    return 1
  }
}

with_lock() {
  local lock="$STATE/lock" n=0
  mkdir_p_state || return 1
  while ! mkdir "$lock" 2>/dev/null; do
    n=$((n + 1))
    if [ "$n" -ge 20 ]; then
      echo "fm-mattermost-outbox-watch: another scan is still running" >&2
      return 0
    fi
    sleep 0.1
  done
  _LOCK_PATH="$lock"
  scan_once; local rc=$?
  rmdir "$lock" 2>/dev/null || true
  _LOCK_PATH=
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

post_key() {
  printf '%s' "$1" | sha256sum | awk '{print $1}'
}

message_file_for() {
  local url=$1 risk=$2 out=$3
  {
    printf 'PR: %s\n' "$url"
    printf 'Risk: %s\n' "$risk"
  } > "$out"
}

post_pr() {
  local file=$1 target=$2 url risk key marker tmp msg rc
  url=$(json_pr_url "$file" 2>/dev/null) || {
    echo "fm-mattermost-outbox-watch: malformed JSON: $file" >&2
    return 0
  }
  [ -n "$url" ] || return 0

  risk=$(json_risk "$file" 2>/dev/null) || risk=unknown
  key=$(post_key "$url")
  marker="$STATE/posted/$key.posted"
  [ -e "$marker" ] && return 0

  msg=$(mktemp "${TMPDIR:-/tmp}/fm-mattermost-outbox.XXXXXX") || {
    echo "fm-mattermost-outbox-watch: cannot create temp message" >&2
    return 1
  }
  message_file_for "$url" "$risk" "$msg"
  hermes send --to "$target" --file "$msg" --quiet
  rc=$?
  rm -f "$msg"
  if [ "$rc" -ne 0 ]; then
    echo "fm-mattermost-outbox-watch: hermes send failed for $file" >&2
    return 1
  fi

  tmp="$marker.tmp.$$"
  {
    printf 'url=%s\n' "$url"
    printf 'risk=%s\n' "$risk"
    printf 'source=%s\n' "$file"
    date -u '+posted_at=%Y-%m-%dT%H:%M:%SZ'
  } > "$tmp" && mv -f "$tmp" "$marker"
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

  local target file
  target=$(hermes_target) || return 1

  find "$OUTBOX" -maxdepth 1 -type f -name '*.json' -print | LC_ALL=C sort | while IFS= read -r file; do
    post_pr "$file" "$target" || exit 1
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
