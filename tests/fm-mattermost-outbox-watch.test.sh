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

test_posts_pr_once_and_records_marker
test_missing_outbox_is_noop_without_hermes
test_malformed_json_does_not_send_or_mark
test_target_resolution_failure_fails_closed
test_explicit_target_skips_target_listing
