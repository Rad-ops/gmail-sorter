#!/usr/bin/env bash
set -euo pipefail

if [[ "${GMAIL_RESCUE_INHIBITED:-0}" != "1" ]]; then
  exec env GMAIL_RESCUE_INHIBITED=1 systemd-inhibit \
    --what=sleep:idle:handle-lid-switch \
    --why="Gmail Trash rescue audit and verified permanent delete" \
    "$0" "$@"
fi

ROOT="/home/rzangeneh/codebase"
SORTER="$ROOT/sorter"
LOG_DIR="$ROOT/local-ai-gmail-interpreter/logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/trash-rescue-delete-verified-$STAMP.log"

mkdir -p "$LOG_DIR"

cd "$SORTER"

echo "Starting Gmail Trash rescue audit + verified permanent delete at $(date -Is)" | tee "$LOG_FILE"
echo "Permanent delete gate: script_delete_confidence=100 AND local model keep_trash confidence=1.0" | tee -a "$LOG_FILE"
echo "Log: $LOG_FILE" | tee -a "$LOG_FILE"

.venv/bin/python src/trash_rescue_audit.py \
  --progress-file data/gmail_sorter_all_years_progress.json \
  --out-prefix reports/trash_rescue_audit_local_qwen \
  --local-llm \
  --start-local-llm \
  --local-llm-profile qwen36 \
  --local-llm-max 0 \
  --llm-export \
  --llm-body-chars 1600 \
  --sleep 0.2 \
  --retries 8 \
  --retry-sleep 8 \
  --http-timeout 180 \
  --local-llm-timeout 240 \
  --delete-passed-trash \
  --i-understand-permanent-delete \
  2>&1 | tee -a "$LOG_FILE"

echo "Completed at $(date -Is)" | tee -a "$LOG_FILE"
echo "Review: $SORTER/reports/trash_rescue_audit_local_qwen.html" | tee -a "$LOG_FILE"
echo "Delete manifest: $SORTER/reports/trash_rescue_audit_local_qwen_permanent_delete_manifest.json" | tee -a "$LOG_FILE"
