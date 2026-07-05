#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/rzangeneh/codebase"
SORTER="$ROOT/sorter"
LOG_DIR="$ROOT/local-ai-gmail-interpreter/logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/trash-rescue-$STAMP.log"

mkdir -p "$LOG_DIR"

cd "$SORTER"

echo "Starting overnight Gmail Trash rescue audit at $(date -Is)" | tee "$LOG_FILE"
echo "Log: $LOG_FILE" | tee -a "$LOG_FILE"

.venv/bin/python src/trash_rescue_audit.py \
  --progress-file data/gmail_sorter_all_years_progress.json \
  --out-prefix reports/trash_rescue_audit_local_qwen \
  --local-llm \
  --start-local-llm \
  --local-llm-profile coder-big \
  --local-llm-max 0 \
  --llm-export \
  --llm-body-chars 1600 \
  --sleep 0.2 \
  --retries 8 \
  --retry-sleep 8 \
  --http-timeout 180 \
  --local-llm-timeout 240 \
  2>&1 | tee -a "$LOG_FILE"

echo "Completed at $(date -Is)" | tee -a "$LOG_FILE"
echo "Review: $SORTER/reports/trash_rescue_audit_local_qwen.html" | tee -a "$LOG_FILE"
