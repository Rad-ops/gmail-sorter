# Overnight Local-Qwen Trash Rescue Runbook

Use this before permanently emptying Gmail Trash.

## Recommended Command

```bash
/home/rzangeneh/codebase/local-ai-gmail-interpreter/commands/run-overnight-trash-rescue.sh
```

This runs:

- Gmail re-fetch of all planned-trash messages from `data/gmail_sorter_all_years_progress.json`.
- Local heuristic rescue audit.
- Local Qwen review through `http://127.0.0.1:8080/v1/chat/completions`.
- HTML/CSV/JSON report generation.
- JSONL packet export for manual local-model retry if needed.

## Output

```text
reports/trash_rescue_audit_local_qwen.html
reports/trash_rescue_audit_local_qwen.csv
reports/trash_rescue_audit_local_qwen.json
reports/trash_rescue_audit_local_qwen_summary.json
reports/trash_rescue_audit_local_qwen_local_llm_results.jsonl
reports/trash_rescue_audit_local_qwen_llm_input.jsonl
reports/trash_rescue_audit_local_qwen_llm_prompt.md
```

Review the HTML first.

## Why Conservative Settings

The mailbox has a long history and the goal is not speed. The wrapper uses:

- `--sleep 0.2`
- `--retries 8`
- `--retry-sleep 8`
- `--http-timeout 180`
- `--local-llm-timeout 240`
- `--llm-body-chars 1600`

This gives Gmail and the local model room to work for an unattended overnight run.

## Restore After Review

Only after reviewing the HTML:

```bash
/home/rzangeneh/codebase/local-ai-gmail-interpreter/commands/restore-reviewed-candidates.sh
```

This untrashes rescue candidates and applies `Trash Rescue/...` labels.
