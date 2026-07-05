# Gmail Sorter

Dashboard-centered Gmail cleanup tool for older mail. It scans messages before December 30, 2025 by default, categorizes them, reports noisy senders and unsubscribable domains, and applies label/archive/trash stages only when explicitly requested.

Current version: `0.3.0` (`20260705`).

## Folder Layout

```text
sorter/
  src/                 Python source
  config/              allowlist and blocklist
  secrets/             Gmail OAuth credentials and tokens, ignored by Git
  reports/             generated dashboard and CSV/JSON reports, ignored by Git
  manifests/           reviewed action manifests, ignored by Git
  data/                resumable progress cache, ignored by Git
  docs/                notes and future documentation
```

## Setup

```bash
cd /home/rzangeneh/codebase/sorter
python3 -m pip install -r requirements.txt
```

Put Gmail API credentials in:

```text
secrets/credentials.json
```

OAuth tokens are generated under `secrets/` and are intentionally ignored by Git.

## First Scan

```bash
python3 src/gmail_sorter.py --resume
```

This creates:

```text
reports/gmail_sorter_report.html
reports/gmail_sorter_report.csv
reports/gmail_sorter_report.json
reports/gmail_sorter_report_senders.csv
reports/gmail_sorter_report_storage.csv
reports/gmail_sorter_report_unsubscribe.csv
manifests/label_manifest.json
manifests/archive_manifest.json
manifests/trash_manifest.json
manifests/review/domain_review.csv
```

Review the HTML dashboard first. The dashboard includes review queues, noisy senders, top sender bulk preview, trash summary by domain, attachment review, perfect ad matches, header unsubscribe domains, and body unsubscribe links.

The sorter also writes a SQLite state database to `data/gmail_sorter_state.sqlite` unless `--disable-state-db` is used. The database keeps the latest decision for each message plus an append-only action ledger for successful label/archive/trash changes.

## Staged Apply

Labels only:

```bash
python3 src/gmail_sorter.py --stage label --apply --resume
```

Archive low-value bulk mail:

```bash
python3 src/gmail_sorter.py --stage archive --apply --resume
```

Trash very high-confidence ads only after reviewing the dashboard:

```bash
python3 src/gmail_sorter.py --stage trash --apply --trash-obvious-ads --i-understand-trash --resume
```

All-years trash apply with combined and yearly dashboards:

```bash
cd /home/rzangeneh/codebase/sorter
.venv/bin/python src/gmail_sorter.py \
  --stage trash \
  --apply \
  --trash-obvious-ads \
  --i-understand-trash \
  --resume \
  --workers 6 \
  --sleep 0.1 \
  --attachment-details \
  --query "in:anywhere -in:trash" \
  --out-prefix reports/gmail_sorter_all_years_trash_apply \
  --progress-file data/gmail_sorter_all_years_progress.json \
  --manifest-dir manifests/all_years
```

The combined dashboard is written to `reports/gmail_sorter_all_years_trash_apply.html`. Per-year dashboards are written beside it with suffixes such as `_2024.html`.

For interrupted all-years trash apply runs, rerun the same command with `--resume` and without `--refresh-existing`. The query excludes Trash, so messages already moved by a previous interrupted run are not selected again.

Apply only a reviewed manifest:

```bash
python3 src/gmail_sorter.py --stage archive --apply --resume --manifest manifests/archive_manifest.json
```

Use a canary trash apply when you want the first small batch to prove the policy before continuing:

```bash
python3 src/gmail_sorter.py \
  --stage trash \
  --apply \
  --trash-obvious-ads \
  --i-understand-trash \
  --resume \
  --canary-limit 100 \
  --max-trash-per-domain 500
```

## Review Workflow

`manifests/review/domain_review.csv` groups messages by registered domain, not noisy subdomains. It includes message counts, planned trash/archive counts, protected counts, real attachment counts, storage size, sample subjects, and a suggested action such as `approve_trash`, `unsubscribe_review`, or `protect_priority`.

Priority mail is labeled and protected when it matches immigration, studies, or real attachment signals. Immigration signals include IRCC/visa/work permit/permanent residence terms and known lawyer/contact names such as Pinaz Marolia, Tiffani, Ronen, Raquel, Jemma, Jonalyn, and Oskoii.

`reports/*_storage.csv` ranks registered domains by estimated Gmail storage usage. Use it to find the few senders that reclaim the most storage without digging through individual messages.

## Maintenance Mode

After the historical cleanup, use maintenance scans for new mail only:

```bash
python3 src/gmail_sorter.py --maintenance-days 30 --resume --attachment-details
```

Or scan from an exact date:

```bash
python3 src/gmail_sorter.py --since-date 2026-07-01 --resume
```

## Trash Rescue Audit

Before permanently emptying Gmail Trash, run a dry-run rescue audit against the messages that the all-years trash command planned to trash:

Recommended overnight local-Qwen run:

```bash
/home/rzangeneh/codebase/local-ai-gmail-interpreter/commands/run-overnight-trash-rescue.sh
```

The same command is also available from this repo:

```bash
commands/run-overnight-trash-rescue.sh
```

Manual dry run without local model:

```bash
cd /home/rzangeneh/codebase/sorter
.venv/bin/python src/trash_rescue_audit.py \
  --progress-file data/gmail_sorter_all_years_progress.json \
  --out-prefix reports/trash_rescue_audit \
  --sleep 0.1 \
  --http-timeout 120
```

This creates:

```text
reports/trash_rescue_audit.html
reports/trash_rescue_audit.csv
reports/trash_rescue_audit.json
reports/trash_rescue_audit_summary.json
```

The audit re-fetches each planned-trash message from Gmail, confirms whether it is still in Trash, and flags possible mistakes using deeper rules for immigration, studies, legal/transactional language, real attachments, conversation signals, and original sorter protection reasons.

Optional model-assisted review:

```bash
OPENAI_API_KEY=... .venv/bin/python src/trash_rescue_audit.py \
  --openai \
  --web-search \
  --openai-max 200
```

The model path is optional and only reviews likely borderline/high-risk candidates. The local heuristic report still works without it.

For a local Qwen model, export bounded review packets instead of giving the model Gmail access:

```bash
.venv/bin/python src/trash_rescue_audit.py \
  --progress-file data/gmail_sorter_all_years_progress.json \
  --out-prefix reports/trash_rescue_audit \
  --llm-export \
  --llm-body-chars 1200
```

Feed these files to Qwen:

```text
reports/trash_rescue_audit_llm_prompt.md
reports/trash_rescue_audit_llm_input.jsonl
```

Qwen should produce JSONL like:

```json
{"message_id":"abc123","decision":"rescue_review","confidence":0.91,"reason":"IRCC/legal document signal","signals":["immigration","attachment"]}
```

Merge Qwen's output without re-fetching Gmail:

```bash
.venv/bin/python src/trash_rescue_audit.py \
  --from-audit-json reports/trash_rescue_audit.json \
  --model-results reports/qwen_trash_rescue_results.jsonl \
  --out-prefix reports/trash_rescue_audit_qwen
```

To run the local Qwen/llama.cpp review automatically through the local OpenAI-compatible server:

```bash
.venv/bin/python src/trash_rescue_audit.py \
  --progress-file data/gmail_sorter_all_years_progress.json \
  --out-prefix reports/trash_rescue_audit_local_qwen \
  --local-llm \
  --start-local-llm \
  --local-llm-profile coder-big \
  --local-llm-max 0 \
  --llm-body-chars 1200 \
  --sleep 0.1 \
  --http-timeout 120
```

This uses `llm-switch coder-big` to start the `local-llm` systemd service and calls `http://127.0.0.1:8080/v1/chat/completions` with model `local`. It writes `reports/trash_rescue_audit_local_qwen_local_llm_results.jsonl` and merges those model decisions into the final HTML/CSV/JSON reports.

The full runbook is in `docs/OVERNIGHT-LOCAL-QWEN-RUNBOOK.md`.

If you decide to restore rescue candidates after reviewing the report:

```bash
.venv/bin/python src/trash_rescue_audit.py \
  --apply \
  --i-understand-restore
```

Restore applies one of these Gmail labels and untrashes the message unless `--label-only` is used:

- `Trash Rescue/Review - 100 Confidence`
- `Trash Rescue/Review - 75-99 Confidence`
- `Trash Rescue/Review - Under 75 Confidence`

## Performance Controls

`--workers` controls parallel read/classification workers. Writes remain sequential and batched.

```bash
python3 src/gmail_sorter.py --resume --workers 8
```

`--sleep` is the base throttle. The script increases delay automatically when Gmail returns retryable quota/rate errors, then gradually recovers after successful requests.

`--http-timeout 120` is the default Gmail HTTP request timeout. Increase it for very slow connections, or lower it if you want stuck requests to fail faster.

`--apply-progress-every 100` controls how often the apply phase prints progress for single-message trash calls and batch label/archive calls.

`--refresh-after-days 7` refreshes cached decisions older than seven days when `--resume` is used. Use `--refresh-existing` to rescan everything.

`--attachment-details` fetches metadata-rich payloads to report attachment filenames and MIME types. It does not download attachment bytes.

It also allows the report to inspect text/html and text/plain message parts for unsubscribe URLs. The script does not persist email body text; it stores only normalized unsubscribe/preference links in reports.

## Safety

The default run is classification only. Gmail changes require `--apply`. Trash requires:

```text
--stage trash --trash-obvious-ads --i-understand-trash
```

Protected messages are kept out of archive/trash when they are allowlisted, important/starred/primary, have real attachments, or match protected categories such as immigration, studies, finance, account security, health, government/legal, utilities, insurance, or receipts/orders. Inline marketing images are tracked separately so image-only promotional mail is not overprotected.

`perfect_ad_match` means the message reached 100 ad confidence, has multiple independent bulk-mail signals such as Gmail promotions, List-Unsubscribe, List-Id, one-click unsubscribe, bulk precedence, or promotional sender local-parts, and has promotional body/subject content. Perfect matches still respect the same protected-message checks and mixed-thread protection.

Trash safety controls:

- `--max-trash-per-domain N` caps trash actions per registered domain.
- `--max-trash-total N` caps the total trash plan.
- `--canary-limit N` limits an apply run to the first N trash actions.

## Tests

```bash
python3 -m unittest discover -s tests
```
