# Overnight Local-Qwen Trash Rescue Runbook

Use this before permanently emptying Gmail Trash.

## Recommended Command

Audit only:

```bash
/home/rzangeneh/codebase/local-ai-gmail-interpreter/commands/run-overnight-trash-rescue.sh
```

Audit plus verified permanent delete:

```bash
/home/rzangeneh/codebase/sorter/commands/run-overnight-trash-rescue-and-delete-verified.sh
```

The verified delete command uses `systemd-inhibit` to prevent sleep and deletes only messages where both gates are exactly 100%:

- sorter/script delete confidence is `100`
- local Qwen decision is `keep_trash` with confidence `1.0`

This runs:

- Gmail re-fetch of all planned-trash messages from `data/gmail_sorter_all_years_progress.json`.
- Local heuristic rescue audit.
- Local Qwen review through `http://127.0.0.1:8080/v1/chat/completions`.
- HTML/CSV/JSON report generation.
- JSONL packet export for manual local-model retry if needed.
- Optional permanent delete manifest and deletion of verified-safe Trash when using the `delete-verified` command.

## Output

```text
reports/trash_rescue_audit_local_qwen.html
reports/trash_rescue_audit_local_qwen.csv
reports/trash_rescue_audit_local_qwen.json
reports/trash_rescue_audit_local_qwen_summary.json
reports/trash_rescue_audit_local_qwen_local_llm_results.jsonl
reports/trash_rescue_audit_local_qwen_llm_input.jsonl
reports/trash_rescue_audit_local_qwen_llm_prompt.md
reports/trash_rescue_audit_local_qwen_missing_gmail_ids.txt
```

Review the HTML first.

If Gmail returns `404 Requested entity was not found` for old message IDs, the script treats those as stale/missing progress entries. It skips them, prints only a short summary, and writes all missing IDs to the missing-ID text file.

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
