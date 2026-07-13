#!/usr/bin/env bash
# Phase A Firstmate Bridge contract, injector, trigger, and relay behavior.
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TMP_ROOT=$(fm_test_tmproot firstmate-bridge)
TEST_TIMEOUT=${FM_BRIDGE_TEST_TIMEOUT:-20}
case "$TEST_TIMEOUT" in ''|*[!0-9]*) fail "FM_BRIDGE_TEST_TIMEOUT must be an integer" ;; esac
command -v timeout >/dev/null 2>&1 || fail "bridge tests require timeout"

timeout "$TEST_TIMEOUT" python3 "$ROOT/tests/firstmate_bridge_test.py" \
  || fail "Python bridge behavior suite failed or timed out"

make_runtime() {
  local runtime=$1 fakebin
  mkdir -p "$runtime/home/data" "$runtime/home/projects/firstmate" "$runtime/home/projects/bracket_report" \
    "$runtime/home/projects/lead-ops-agent" "$runtime/home/state" "$runtime/log"
  printf '%s\n' \
    '- firstmate [no-mistakes] - test' \
    '- bracket_report [no-mistakes] - test' \
    '- lead-ops-agent [no-mistakes] - test' > "$runtime/home/data/projects.md"
  printf 'Build the requested feature and tests.\n' > "$runtime/brief.md"
  fakebin=$(fm_fakebin "$runtime")
  cat > "$fakebin/tmux" <<'SH'
#!/usr/bin/env bash
set -u
case "${1:-}" in
  has-session)
    [ "${FM_TEST_CAPTAIN:-present}" = present ]
    ;;
  list-panes)
    printf 'fm:captain.0\tcaptain\t%s\t4242\n' "$FM_HOME"
    ;;
  display-message)
    if [ "${FM_TEST_ACK:-empty}" = unknown ] && [ -e "$FM_TEST_RUNTIME/log/literal-sent" ]; then
      exit 1
    fi
    case "$*" in
      *'#{cursor_y}'*) printf '0\n' ;;
      *) printf '%%0\n' ;;
    esac
    ;;
  capture-pane)
    case "$*" in
      *' -e '*|*' -e'*)
        if [ "${FM_TEST_COMPOSER:-empty}" = pending ]; then printf '> human draft\n'; else printf '>\n'; fi
        ;;
      *)
        if [ "${FM_TEST_BUSY:-idle}" = busy ]; then printf 'esc to interrupt\n'; else printf 'idle\n'; fi
        ;;
    esac
    ;;
  send-keys)
    case "$*" in
      *' -l '*)
        : > "$FM_TEST_RUNTIME/log/literal-sent"
        printf '%s\n' "$*" >> "$FM_TEST_RUNTIME/log/send.log"
        ;;
    esac
    ;;
  list-windows) exit 0 ;;
  *) exit 0 ;;
esac
SH
cat > "$fakebin/pstree" <<'SH'
#!/usr/bin/env bash
printf 'codex,4243\n'
SH
  cat > "$fakebin/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x "$fakebin/tmux" "$fakebin/pstree" "$fakebin/sleep"
}

run_injector() {
  local runtime=$1 card=$2
  shift 2
  timeout "$TEST_TIMEOUT" env PATH="$runtime/fakebin:$PATH" FM_HOME="$runtime/home" \
    FM_ROOT_OVERRIDE="$runtime/home" FM_TEST_RUNTIME="$runtime" FM_BRIDGE_SEND_SETTLE=0 \
    "$ROOT/bin/fm-bridge-inject.sh" \
      --repo firstmate --brief-path "$runtime/brief.md" --card-id "$card" --mode ship \
      --canonical-thread thread-123 "$@"
}

test_double_call_is_card_idempotent() {
  local runtime="$TMP_ROOT/double" card="d2345678901234567890123456" first second
  make_runtime "$runtime"
  first=$(run_injector "$runtime" "$card") || fail "first injector call should send"
  second=$(run_injector "$runtime" "$card") || fail "second injector call should dedupe"
  [ "$(printf '%s' "$first" | jq -r .state)" = sent ] || fail "first call must be sent"
  [ "$(printf '%s' "$second" | jq -r .state)" = sent ] || fail "second call must report durable sent"
  [ "$(wc -l < "$runtime/log/send.log")" = 1 ] || fail "same card must be typed exactly once"
  [ "$(jq -s '[.[]|select(.state=="sent")]|length' "$runtime/home/state/bridge/dispatch-ledger.jsonl")" = 1 ] \
    || fail "ledger must have exactly one sent state"
  pass "injector double-call is card-idempotent"
}

test_absent_captain_fails_closed() {
  local runtime="$TMP_ROOT/absent" card="e2345678901234567890123456" out rc
  make_runtime "$runtime"
  set +e
  out=$(FM_TEST_CAPTAIN=absent run_injector "$runtime" "$card")
  rc=$?
  set -e
  [ "$rc" -ne 0 ] || fail "absent captain must fail"
  [ "$(printf '%s' "$out" | jq -r .state)" = failed-before-send ] || fail "absent captain state must fail-before-send"
  [ "$(tail -1 "$runtime/home/state/bridge/dispatch-ledger.jsonl" | jq -r .state)" = failed-before-send ] \
    || fail "ledger must record failed-before-send"
  assert_absent "$runtime/log/send.log" "absent captain must not send"
  pass "injector fails closed when captain is absent"
}

test_unknown_repo_and_multiline_are_refused() {
  local runtime="$TMP_ROOT/refuse" card="f2345678901234567890123456"
  make_runtime "$runtime"
  if timeout "$TEST_TIMEOUT" env PATH="$runtime/fakebin:$PATH" FM_HOME="$runtime/home" \
    FM_ROOT_OVERRIDE="$runtime/home" FM_TEST_RUNTIME="$runtime" \
    "$ROOT/bin/fm-bridge-inject.sh" --repo symbol_lookup --brief-path "$runtime/brief.md" \
      --card-id "$card" --mode ship --canonical-thread thread-123 >/dev/null 2>&1; then
    fail "unknown repo must be refused"
  fi
  if run_injector "$runtime" "$card" --target-channel $'Agentic\nDevelopment' >/dev/null 2>&1; then
    fail "multiline routing input must be refused"
  fi
  assert_absent "$runtime/log/send.log" "rejected input must not send"
  pass "injector refuses unknown repos and multiline input"
}

test_kill_switch_precedes_claim() {
  local runtime="$TMP_ROOT/paused" card="g2345678901234567890123456"
  make_runtime "$runtime"
  mkdir -p "$runtime/home/state/bridge"
  : > "$runtime/home/state/bridge/PAUSED"
  run_injector "$runtime" "$card" >/dev/null 2>&1 && fail "kill switch must stop injector"
  assert_absent "$runtime/home/state/bridge/dispatch-ledger.jsonl" "kill switch must halt before claim"
  assert_absent "$runtime/log/send.log" "kill switch must halt before send"
  pass "injector kill switch halts before claim and send"
}

test_pending_composer_and_busy_pane_fail_closed() {
  local runtime="$TMP_ROOT/composer" card="k2345678901234567890123456" out
  make_runtime "$runtime"
  out=$(FM_TEST_COMPOSER=pending run_injector "$runtime" "$card" 2>/dev/null) || true
  [ "$(printf '%s' "$out" | jq -r .state)" = failed-before-send ] \
    || fail "pending composer must fail-before-send"
  assert_absent "$runtime/log/send.log" "pending human composer must not send"

  rm -f "$runtime/home/state/bridge/dispatch-ledger.jsonl" "$runtime/home/state/bridge/injector.jsonl"
  out=$(FM_TEST_BUSY=busy run_injector "$runtime" "$card" 2>/dev/null) || true
  [ "$(printf '%s' "$out" | jq -r .state)" = failed-before-send ] \
    || fail "busy captain must fail-before-send"
  assert_absent "$runtime/log/send.log" "busy captain pane must not send"
  pass "injector refuses pending human composers and busy captain panes"
}

test_protected_intent_matrix() {
  local runtime="$TMP_ROOT/protected" card="m2345678901234567890123456" intent
  make_runtime "$runtime"
  for intent in \
    'Deploy the application.' \
    'Merge the pull request.' \
    'Change the main branch.' \
    'Rotate production secrets.' \
    'Update DNS.' \
    'Delete customer data.'; do
    printf '%s\n' "$intent" > "$runtime/brief.md"
    run_injector "$runtime" "$card" >/dev/null 2>&1 \
      && fail "protected intent was accepted: $intent"
  done
  assert_absent "$runtime/home/state/bridge/dispatch-ledger.jsonl" "protected intent must be refused before claim"
  assert_absent "$runtime/log/send.log" "protected intent must never send"
  pass "injector refuses deploy, merge, main, production, secrets, DNS, and destructive intent"
}

test_phase_a_allows_only_one_active_bridge_card() {
  local runtime="$TMP_ROOT/one-card" first="n2345678901234567890123456" second="p2345678901234567890123456"
  make_runtime "$runtime"
  run_injector "$runtime" "$first" >/dev/null || fail "first bridge card should send"
  run_injector "$runtime" "$second" >/dev/null 2>&1 && fail "second active bridge card must be refused"
  [ "$(wc -l < "$runtime/log/send.log")" = 1 ] || fail "Phase A must send only one active card"
  pass "injector enforces the Phase A one-active-card cap"
}

test_authoritative_live_task_cap() {
  local runtime="$TMP_ROOT/cap" card="j2345678901234567890123456" i
  make_runtime "$runtime"
  for i in $(seq 1 10); do
    printf 'window=fm:task-%s\nkind=ship\nworktree=%s/home/projects/firstmate\n' \
      "$i" "$runtime" > "$runtime/home/state/task-$i.meta"
  done
  run_injector "$runtime" "$card" >/dev/null 2>&1 && fail "eleventh live task must be refused"
  assert_absent "$runtime/log/send.log" "task cap refusal must not send"
  pass "injector enforces the authoritative live-task cap of 10"
}

test_uncertain_ack_never_retypes() {
  local runtime="$TMP_ROOT/uncertain" card="h2345678901234567890123456" out rc
  make_runtime "$runtime"
  set +e
  out=$(FM_TEST_ACK=unknown run_injector "$runtime" "$card")
  rc=$?
  set -e
  [ "$rc" = 3 ] || fail "unknown strict acknowledgement must exit 3"
  [ "$(printf '%s' "$out" | jq -r .state)" = uncertain ] || fail "unknown acknowledgement must be uncertain"
  FM_TEST_ACK=empty run_injector "$runtime" "$card" >/dev/null || fail "repeat uncertain call should dedupe safely"
  [ "$(wc -l < "$runtime/log/send.log")" = 1 ] || fail "uncertain delivery must never auto-retype"
  pass "strict uncertain acknowledgement never auto-retypes"
}

test_double_call_is_card_idempotent
test_absent_captain_fails_closed
test_unknown_repo_and_multiline_are_refused
test_kill_switch_precedes_claim
test_pending_composer_and_busy_pane_fail_closed
test_protected_intent_matrix
test_phase_a_allows_only_one_active_bridge_card
test_authoritative_live_task_cap
test_uncertain_ack_never_retypes
