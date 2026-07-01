#!/usr/bin/env bash
# tests/fm-mattermost-outbox-watch.test.sh - Mattermost outbox watcher behavior.
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TMP_ROOT=$(fm_test_tmproot fm-mattermost-outbox)

make_hermes_stub() {
  local fakebin=$1 log=$2 list_json=$3
  cat > "$fakebin/hermes" <<SH
#!/usr/bin/env bash
set -u
log=$log
list_json=$list_json
if [ "\${1:-}" = send ] && [ "\${2:-}" = --list ]; then
  cat "\$list_json"
  exit 0
fi
if [ "\${1:-}" = send ]; then
  shift
  to=
  file=
  while [ "\$#" -gt 0 ]; do
    case "\$1" in
      --to|-t) shift; to=\${1:-} ;;
      --file|-f) shift; file=\${1:-} ;;
      --quiet|-q) : ;;
    esac
    shift || true
  done
  {
    printf 'TO=%s\n' "\$to"
    printf 'BODY<<EOF\n'
    cat "\$file"
    printf 'EOF\n'
  } >> "\$log"
  exit 0
fi
exit 2
SH
  chmod +x "$fakebin/hermes"
}

make_curl_stub() {
  local fakebin=$1 dir=$2
  mkdir -p "$dir"
  cat > "$fakebin/curl" <<SH
#!/usr/bin/env bash
set -u
dir=$dir
nfile="\$dir/count"
n=0
[ -f "\$nfile" ] && n=\$(cat "\$nfile")
n=\$((n + 1))
printf '%s\n' "\$n" > "\$nfile"
method=
data=
url=
{
  printf 'ARGS'
  for arg in "\$@"; do printf ' %s' "\$arg"; done
  printf '\n'
} > "\$dir/call-\$n.txt"
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    -X) shift; method=\${1:-} ;;
    -H)
      shift
      printf 'HEADER=%s\n' "\${1:-}" >> "\$dir/call-\$n.txt"
      ;;
    --data)
      shift
      data=\${1:-}
      ;;
    -*) : ;;
    *) url=\$1 ;;
  esac
  shift || true
done
printf 'METHOD=%s\n' "\$method" >> "\$dir/call-\$n.txt"
printf 'URL=%s\n' "\$url" >> "\$dir/call-\$n.txt"
printf '%s\n' "\$data" > "\$dir/body-\$n.json"
exit 0
SH
  chmod +x "$fakebin/curl"
}

write_target_list() {
  local file=$1
  cat > "$file" <<'JSON'
{
  "platforms": {
    "mattermost": [
      {
        "id": "post123:thread456",
        "name": "SG AI Coordination",
        "type": "channel",
        "thread_id": "thread456"
      }
    ]
  }
}
JSON
}

test_posts_pr_once_and_records_marker() {
  local w="$TMP_ROOT/once" fakebin log targets
  mkdir -p "$w/home/data/outbox" "$w/home/state"
  fakebin=$(fm_fakebin "$w")
  log="$w/hermes.log"
  targets="$w/targets.json"
  : > "$log"
  write_target_list "$targets"
  make_hermes_stub "$fakebin" "$log" "$targets"
  cat > "$w/home/data/outbox/pr.json" <<'JSON'
{"type":"pr","pr_url":"https://github.com/kunchenguid/firstmate/pull/123","risk":"low"}
JSON

  PATH="$fakebin:$PATH" FM_HOME="$w/home" "$ROOT/bin/fm-mattermost-outbox-watch.sh" \
    || fail "watcher failed to post PR"
  PATH="$fakebin:$PATH" FM_HOME="$w/home" "$ROOT/bin/fm-mattermost-outbox-watch.sh" \
    || fail "watcher failed on duplicate scan"

  [ "$(grep -c '^TO=' "$log")" = 1 ] || fail "watcher must not duplicate posts across restarts"
  assert_grep "TO=mattermost:post123:thread456" "$log" "watcher must send to resolved Mattermost thread"
  assert_grep "PR: https://github.com/kunchenguid/firstmate/pull/123" "$log" "message must include PR URL"
  assert_grep "Risk: low" "$log" "message must include risk"
  find "$w/home/state/mattermost-outbox/posted" -type f -name '*.posted' | grep . >/dev/null \
    || fail "watcher must record a durable posted marker"
  pass "Mattermost outbox watcher posts a PR once and records a durable marker"
}

test_missing_outbox_is_noop_without_hermes() {
  local w="$TMP_ROOT/missing"
  mkdir -p "$w/home"
  FM_HOME="$w/home" PATH="/usr/bin:/bin" "$ROOT/bin/fm-mattermost-outbox-watch.sh" \
    || fail "missing outbox should be a safe no-op even when hermes is absent"
  pass "missing outbox is a safe no-op"
}

test_malformed_json_does_not_send_or_mark() {
  local w="$TMP_ROOT/malformed" fakebin log targets
  mkdir -p "$w/home/data/outbox" "$w/home/state"
  fakebin=$(fm_fakebin "$w")
  log="$w/hermes.log"
  targets="$w/targets.json"
  : > "$log"
  write_target_list "$targets"
  make_hermes_stub "$fakebin" "$log" "$targets"
  printf '{not json\n' > "$w/home/data/outbox/bad.json"

  PATH="$fakebin:$PATH" FM_HOME="$w/home" "$ROOT/bin/fm-mattermost-outbox-watch.sh" \
    2>"$w/err" || fail "malformed JSON should not crash the watcher"
  assert_grep "malformed JSON" "$w/err" "malformed JSON should be reported"
  [ ! -s "$log" ] || fail "malformed JSON must not be sent"
  if find "$w/home/state/mattermost-outbox/posted" -type f -name '*.posted' | grep . >/dev/null; then
    fail "malformed JSON must not create posted markers"
  fi
  pass "malformed JSON fails safely without sending"
}

test_target_resolution_failure_fails_closed() {
  local w="$TMP_ROOT/no-target" fakebin log targets
  mkdir -p "$w/home/data/outbox" "$w/home/state"
  fakebin=$(fm_fakebin "$w")
  log="$w/hermes.log"
  targets="$w/targets.json"
  : > "$log"
  printf '{"platforms":{"mattermost":[]}}\n' > "$targets"
  make_hermes_stub "$fakebin" "$log" "$targets"
  cat > "$w/home/data/outbox/pr.json" <<'JSON'
{"pr_url":"https://github.com/kunchenguid/firstmate/pull/124","risk":"medium"}
JSON

  if PATH="$fakebin:$PATH" FM_HOME="$w/home" "$ROOT/bin/fm-mattermost-outbox-watch.sh" 2>"$w/err"; then
    fail "unresolved Mattermost target should fail closed"
  fi
  assert_grep "cannot resolve Mattermost target" "$w/err" "target failure should explain the problem"
  [ ! -s "$log" ] || fail "unresolved target must not send"
  pass "unresolved Mattermost target fails closed"
}

test_explicit_target_skips_target_listing() {
  local w="$TMP_ROOT/explicit" fakebin log
  mkdir -p "$w/home/data/outbox" "$w/home/state"
  fakebin=$(fm_fakebin "$w")
  log="$w/hermes.log"
  : > "$log"
  cat > "$fakebin/hermes" <<SH
#!/usr/bin/env bash
set -u
log=$log
if [ "\${1:-}" = send ] && [ "\${2:-}" = --list ]; then
  exit 9
fi
shift
to=
file=
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    --to|-t) shift; to=\${1:-} ;;
    --file|-f) shift; file=\${1:-} ;;
    --quiet|-q) : ;;
  esac
  shift || true
done
printf 'TO=%s\n' "\$to" >> "\$log"
cat "\$file" >> "\$log"
exit 0
SH
  chmod +x "$fakebin/hermes"
  cat > "$w/home/data/outbox/pr.json" <<'JSON'
{"html_url":"https://github.com/kunchenguid/firstmate/pull/125","risk":"high"}
JSON

  PATH="$fakebin:$PATH" FM_HOME="$w/home" FM_MATTERMOST_TARGET="mattermost:direct:thread" \
    "$ROOT/bin/fm-mattermost-outbox-watch.sh" || fail "explicit target should send without listing"
  assert_grep "TO=mattermost:direct:thread" "$log" "explicit target should be used directly"
  pass "explicit Mattermost target bypasses target listing"
}

test_target_channel_and_summary_skip_fallback_resolution() {
  local w="$TMP_ROOT/channel" fakebin log
  mkdir -p "$w/home/data/outbox" "$w/home/state"
  fakebin=$(fm_fakebin "$w")
  log="$w/hermes.log"
  : > "$log"
  cat > "$fakebin/hermes" <<SH
#!/usr/bin/env bash
set -u
log=$log
if [ "\${1:-}" = send ] && [ "\${2:-}" = --list ]; then
  exit 9
fi
shift
to=
file=
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    --to|-t) shift; to=\${1:-} ;;
    --file|-f) shift; file=\${1:-} ;;
    --quiet|-q) : ;;
  esac
  shift || true
done
printf 'TO=%s\n' "\$to" >> "\$log"
cat "\$file" >> "\$log"
exit 0
SH
  chmod +x "$fakebin/hermes"
  cat > "$w/home/data/outbox/pr.json" <<'JSON'
{
  "task_id": "task-1",
  "pr_url": "https://github.com/kunchenguid/firstmate/pull/126",
  "risk": "low",
  "summary": "Watcher now syncs task destinations.",
  "target_channel_id": "channelabc"
}
JSON

  PATH="$fakebin:$PATH" FM_HOME="$w/home" "$ROOT/bin/fm-mattermost-outbox-watch.sh" \
    || fail "target_channel_id entry should post without fallback target lookup"
  assert_grep "TO=mattermost:channelabc" "$log" "target_channel_id should become the Mattermost target"
  assert_grep "Summary: Watcher now syncs task destinations." "$log" "message must include summary"
  assert_grep "PR: https://github.com/kunchenguid/firstmate/pull/126" "$log" "message must include PR"
  assert_grep "Risk: low" "$log" "message must include risk"
  pass "target_channel_id and summary fields are parsed and used"
}

test_focalboard_comment_and_move_requests() {
  local w="$TMP_ROOT/focalboard" fakebin log curl_log
  mkdir -p "$w/home/data/outbox" "$w/home/state"
  fakebin=$(fm_fakebin "$w")
  log="$w/hermes.log"
  curl_log="$w/curl"
  : > "$log"
  make_curl_stub "$fakebin" "$curl_log"
  cat > "$fakebin/hermes" <<SH
#!/usr/bin/env bash
set -u
log=$log
shift
to=
file=
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    --to|-t) shift; to=\${1:-} ;;
    --file|-f) shift; file=\${1:-} ;;
    --quiet|-q) : ;;
  esac
  shift || true
done
printf 'TO=%s\n' "\$to" >> "\$log"
cat "\$file" >> "\$log"
exit 0
SH
  chmod +x "$fakebin/hermes"
  cat > "$w/home/data/outbox/pr.json" <<'JSON'
{
  "task_id": "task-2",
  "pr_url": "https://github.com/kunchenguid/firstmate/pull/127",
  "risk": "medium",
  "summary": "Card sync is ready.",
  "target_channel_id": "channeldef",
  "board_id": "board123",
  "card_id": "card456",
  "new_status": "QA / Review"
}
JSON

  PATH="$fakebin:$PATH" FM_HOME="$w/home" \
    FM_FOCALBOARD_URL="https://boards.example.test/plugins/focalboard/api/v1/" \
    FM_FOCALBOARD_TOKEN="dummy-token" \
    "$ROOT/bin/fm-mattermost-outbox-watch.sh" || fail "Focalboard sync should succeed with stubbed curl"

  [ "$(cat "$curl_log/count")" = 2 ] || fail "Focalboard sync should make one comment call and one status move call"
  assert_grep "METHOD=POST" "$curl_log/call-1.txt" "comment call should use POST"
  assert_grep "URL=https://boards.example.test/plugins/focalboard/api/v1/boards/board123/blocks?disable_notify=true" "$curl_log/call-1.txt" "comment call should use the board blocks route"
  assert_grep "HEADER=X-Requested-With: XMLHttpRequest" "$curl_log/call-1.txt" "comment call must include X-Requested-With"
  jq -e '.[0].type == "comment" and .[0].boardId == "board123" and .[0].parentId == "card456"' "$curl_log/body-1.json" >/dev/null \
    || fail "comment payload should create a comment block under the card"
  jq -e '.[0].title | contains("Summary: Card sync is ready.") and contains("PR: https://github.com/kunchenguid/firstmate/pull/127") and contains("Risk: medium")' "$curl_log/body-1.json" >/dev/null \
    || fail "comment payload should include summary, PR, and risk"
  assert_grep "METHOD=PATCH" "$curl_log/call-2.txt" "status move should use PATCH"
  assert_grep "URL=https://boards.example.test/plugins/focalboard/api/v1/cards/card456?disable_notify=true" "$curl_log/call-2.txt" "status move should use the card patch route"
  assert_grep "HEADER=X-Requested-With: XMLHttpRequest" "$curl_log/call-2.txt" "status move must include X-Requested-With"
  jq -e '.updatedProperties.status == "QA / Review"' "$curl_log/body-2.json" >/dev/null \
    || fail "status move payload should set the status property"
  pass "Focalboard comment and move request construction is correct"
}

test_missing_focalboard_credentials_warns_but_posts_mattermost() {
  local w="$TMP_ROOT/no-focal-creds" fakebin log err
  mkdir -p "$w/home/data/outbox" "$w/home/state"
  fakebin=$(fm_fakebin "$w")
  log="$w/hermes.log"
  err="$w/err"
  : > "$log"
  cat > "$fakebin/hermes" <<SH
#!/usr/bin/env bash
set -u
log=$log
shift
to=
file=
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    --to|-t) shift; to=\${1:-} ;;
    --file|-f) shift; file=\${1:-} ;;
    --quiet|-q) : ;;
  esac
  shift || true
done
printf 'TO=%s\n' "\$to" >> "\$log"
cat "\$file" >> "\$log"
exit 0
SH
  chmod +x "$fakebin/hermes"
  cat > "$w/home/data/outbox/pr.json" <<'JSON'
{
  "pr_url": "https://github.com/kunchenguid/firstmate/pull/128",
  "risk": "high",
  "summary": "Needs credentials to sync card.",
  "target_channel_id": "channelghi",
  "board_id": "board123",
  "card_id": "card789",
  "new_status": "Blocked"
}
JSON

  PATH="$fakebin:$PATH" FM_HOME="$w/home" "$ROOT/bin/fm-mattermost-outbox-watch.sh" 2>"$err" \
    || fail "missing Focalboard credentials should not fail the scan"
  assert_grep "warning: Focalboard card sync requested" "$err" "missing credentials should log a clear warning"
  assert_grep "FM_FOCALBOARD_URL or FM_FOCALBOARD_TOKEN is missing" "$err" "warning should name the required env vars"
  assert_grep "TO=mattermost:channelghi" "$log" "Mattermost post should still be sent"
  pass "missing Focalboard credentials warn without blocking Mattermost"
}

test_duplicate_scan_suppresses_mattermost_comment_and_move() {
  local w="$TMP_ROOT/duplicate-focalboard" fakebin log curl_log
  mkdir -p "$w/home/data/outbox" "$w/home/state"
  fakebin=$(fm_fakebin "$w")
  log="$w/hermes.log"
  curl_log="$w/curl"
  : > "$log"
  make_curl_stub "$fakebin" "$curl_log"
  cat > "$fakebin/hermes" <<SH
#!/usr/bin/env bash
set -u
log=$log
shift
to=
file=
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    --to|-t) shift; to=\${1:-} ;;
    --file|-f) shift; file=\${1:-} ;;
    --quiet|-q) : ;;
  esac
  shift || true
done
printf 'TO=%s\n' "\$to" >> "\$log"
cat "\$file" >> "\$log"
exit 0
SH
  chmod +x "$fakebin/hermes"
  cat > "$w/home/data/outbox/pr.json" <<'JSON'
{
  "pr_url": "https://github.com/kunchenguid/firstmate/pull/129",
  "risk": "low",
  "summary": "Duplicate suppression check.",
  "target_channel_id": "channeljkl",
  "board_id": "board999",
  "card_id": "card999",
  "new_status": "Done"
}
JSON

  PATH="$fakebin:$PATH" FM_HOME="$w/home" \
    FM_FOCALBOARD_URL="https://boards.example.test/plugins/focalboard/api/v1" \
    FM_FOCALBOARD_TOKEN="dummy-token" \
    "$ROOT/bin/fm-mattermost-outbox-watch.sh" || fail "first duplicate test scan failed"
  PATH="$fakebin:$PATH" FM_HOME="$w/home" \
    FM_FOCALBOARD_URL="https://boards.example.test/plugins/focalboard/api/v1" \
    FM_FOCALBOARD_TOKEN="dummy-token" \
    "$ROOT/bin/fm-mattermost-outbox-watch.sh" || fail "second duplicate test scan failed"

  [ "$(grep -c '^TO=' "$log")" = 1 ] || fail "duplicate scan should not double-post to Mattermost"
  [ "$(cat "$curl_log/count")" = 2 ] || fail "duplicate scan should not double-comment or double-move"
  pass "duplicate scans suppress Mattermost and Focalboard side effects"
}

test_posts_pr_once_and_records_marker
test_missing_outbox_is_noop_without_hermes
test_malformed_json_does_not_send_or_mark
test_target_resolution_failure_fails_closed
test_explicit_target_skips_target_listing
test_target_channel_and_summary_skip_fallback_resolution
test_focalboard_comment_and_move_requests
test_missing_focalboard_credentials_warns_but_posts_mattermost
test_duplicate_scan_suppresses_mattermost_comment_and_move
