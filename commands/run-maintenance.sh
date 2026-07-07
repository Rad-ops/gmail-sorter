#!/usr/bin/env bash
# v0.8 weekly maintenance run for the Gmail sorter.
#
# This script is designed to be invoked by a systemd user timer. It
# runs the incremental scan via the Gmail History API, which is ~100x
# faster than a full re-scan on a typical weekly cadence.
#
# Install as a systemd user timer:
#
#   mkdir -p ~/.config/systemd/user
#   cp commands/gmail-sorter-maintenance.service \
#      commands/gmail-sorter-maintenance.timer \
#      ~/.config/systemd/user/
#   systemctl --user daemon-reload
#   systemctl --user enable --now gmail-sorter-maintenance.timer
#
# Logs go to data/runs/ (one file per invocation).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

# Activate the virtualenv if it exists.
if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# Run the incremental scan. The sorter reads the stored last_history_id
# from the SQLite state, fetches the new events via the History API,
# and updates the in-memory decisions accordingly. On a stale history
# id, it falls back to a full re-scan and records the new id.
.venv/bin/python src/gmail_sorter.py \
    --since-history-id auto \
    --resume \
    --stage label \
    --use-embeddings \
    --use-thread-modeling \
    --use-sender-reputation \
    --use-learned-weights

echo "gmail-sorter maintenance run finished at $(date -Iseconds)"
