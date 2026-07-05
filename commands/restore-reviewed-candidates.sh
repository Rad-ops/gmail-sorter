#!/usr/bin/env bash
set -euo pipefail

cd /home/rzangeneh/codebase/sorter

.venv/bin/python src/trash_rescue_audit.py \
  --from-audit-json reports/trash_rescue_audit_local_qwen.json \
  --apply \
  --i-understand-restore
