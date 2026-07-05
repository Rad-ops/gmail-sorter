#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/rzangeneh/codebase"
SORTER="$ROOT/sorter"
LOG_DIR="$ROOT/local-ai-gmail-interpreter/logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
INTERVAL="${INTERVAL:-60}"
MODE="${1:-}"

RUN_LOG="${RUN_LOG:-$LOG_DIR/trash-rescue-delete-verified-20260705-012556.log}"
RESULTS="${RESULTS:-$SORTER/reports/trash_rescue_audit_local_qwen_local_llm_results.jsonl}"
LISTENER_LOG="${LISTENER_LOG:-$LOG_DIR/local-llm-speed-listener-$STAMP.log}"
CSV_LOG="${CSV_LOG:-$LOG_DIR/local-llm-speed-listener-$STAMP.csv}"

mkdir -p "$LOG_DIR"

if [[ ! -f "$CSV_LOG" ]]; then
  printf '%s\n' 'timestamp,audit_running,llm_running,reviewed_messages,latest_review_progress,prompt_tokens,prompt_tokens_per_second,generation_tokens,generation_tokens_per_second,total_tokens,total_ms' > "$CSV_LOG"
fi

json_quote() {
  printf '%s' "$1" | sed 's/"/'\''/g; s/,/ /g'
}

snapshot() {
  local now audit_running llm_running reviewed latest_review journal prompt_line eval_line total_line
  local prompt_tokens prompt_tps generation_tokens generation_tps total_tokens total_ms

  now="$(date -Is)"
  audit_running="no"
  llm_running="no"
  reviewed="0"
  latest_review=""
  prompt_tokens=""
  prompt_tps=""
  generation_tokens=""
  generation_tps=""
  total_tokens=""
  total_ms=""

  if pgrep -af 'src/trash_rescue_audit.py' >/dev/null 2>&1; then
    audit_running="yes"
  fi
  if systemctl --user is-active --quiet local-llm.service 2>/dev/null; then
    llm_running="yes"
  fi
  if [[ -f "$RESULTS" ]]; then
    reviewed="$(wc -l < "$RESULTS" | tr -d ' ')"
  fi
  if [[ -f "$RUN_LOG" ]]; then
    latest_review="$(grep -E 'Local LLM reviewed [0-9]+/[0-9]+ candidates|Audited [0-9]+/[0-9]+ candidates' "$RUN_LOG" | tail -1 || true)"
  fi

  journal="$(journalctl --user -u local-llm.service --since '2 minutes ago' --no-pager 2>/dev/null || true)"
  prompt_line="$(printf '%s\n' "$journal" | grep 'prompt eval time' | tail -1 || true)"
  eval_line="$(printf '%s\n' "$journal" | grep ' eval time' | tail -1 || true)"
  total_line="$(printf '%s\n' "$journal" | grep ' total time' | tail -1 || true)"

  if [[ -n "$prompt_line" ]]; then
    prompt_tokens="$(printf '%s\n' "$prompt_line" | sed -n 's|.* / *\([0-9][0-9]*\) tokens .*|\1|p')"
    prompt_tps="$(printf '%s\n' "$prompt_line" | sed -n 's|.* \([0-9][0-9.]*\) tokens per second).*|\1|p')"
  fi
  if [[ -n "$eval_line" ]]; then
    generation_tokens="$(printf '%s\n' "$eval_line" | sed -n 's|.* / *\([0-9][0-9]*\) tokens .*|\1|p')"
    generation_tps="$(printf '%s\n' "$eval_line" | sed -n 's|.* \([0-9][0-9.]*\) tokens per second).*|\1|p')"
  fi
  if [[ -n "$total_line" ]]; then
    total_ms="$(printf '%s\n' "$total_line" | sed -n 's|.*total time = *\([0-9][0-9.]*\) ms /.*|\1|p')"
    total_tokens="$(printf '%s\n' "$total_line" | sed -n 's|.* / *\([0-9][0-9]*\) tokens.*|\1|p')"
  fi

  {
    printf '[%s]\n' "$now"
    printf '  audit_running=%s llm_running=%s reviewed_messages=%s\n' "$audit_running" "$llm_running" "$reviewed"
    if [[ -n "$latest_review" ]]; then
      printf '  progress=%s\n' "$latest_review"
    fi
    if [[ -n "$total_tokens" || -n "$generation_tps" ]]; then
      printf '  latest_request_total_tokens=%s total_ms=%s\n' "${total_tokens:-unknown}" "${total_ms:-unknown}"
      printf '  prompt_tokens=%s prompt_tps=%s\n' "${prompt_tokens:-unknown}" "${prompt_tps:-unknown}"
      printf '  generation_tokens=%s generation_tps=%s\n' "${generation_tokens:-unknown}" "${generation_tps:-unknown}"
    else
      printf '  llm_speed=waiting_for_local_llm_timing_lines\n'
    fi
    printf '\n'
  } >> "$LISTENER_LOG"

  printf '"%s","%s","%s","%s","%s","%s","%s","%s","%s","%s","%s"\n' \
    "$now" \
    "$audit_running" \
    "$llm_running" \
    "$reviewed" \
    "$(json_quote "$latest_review")" \
    "$prompt_tokens" \
    "$prompt_tps" \
    "$generation_tokens" \
    "$generation_tps" \
    "$total_tokens" \
    "$total_ms" >> "$CSV_LOG"
}

echo "Local LLM speed listener started at $(date -Is)" >> "$LISTENER_LOG"
echo "Interval: ${INTERVAL}s" >> "$LISTENER_LOG"
echo "Run log: $RUN_LOG" >> "$LISTENER_LOG"
echo "Results: $RESULTS" >> "$LISTENER_LOG"
echo "CSV: $CSV_LOG" >> "$LISTENER_LOG"
echo >> "$LISTENER_LOG"

while true; do
  snapshot
  if [[ "$MODE" == "--once" ]]; then
    break
  fi
  sleep "$INTERVAL"
done
