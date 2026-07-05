# Gmail Sorter

Dashboard-centered Gmail cleanup tool for older mail. It scans messages before December 30, 2025 by default, categorizes them, reports noisy senders and unsubscribable domains, and applies label/archive/trash stages only when explicitly requested.

Current version: `0.2.0` (`20260705`).

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
reports/gmail_sorter_report_unsubscribe.csv
manifests/label_manifest.json
manifests/archive_manifest.json
manifests/trash_manifest.json
```

Review the HTML dashboard first. The dashboard includes review queues, noisy senders, top sender bulk preview, trash summary by domain, attachment review, perfect ad matches, header unsubscribe domains, and body unsubscribe links.

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

Protected messages are kept out of archive/trash when they are allowlisted, important/starred/primary, have attachments, or match protected categories such as finance, account security, health, government/legal, utilities, insurance, or receipts/orders.

`perfect_ad_match` means the message reached 100 ad confidence, has multiple independent bulk-mail signals such as Gmail promotions, List-Unsubscribe, List-Id, one-click unsubscribe, bulk precedence, or promotional sender local-parts, and has promotional body/subject content. Perfect matches still respect the same protected-message checks and mixed-thread protection.
