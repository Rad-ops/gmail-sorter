# Next-Run Handoff

Updated: 2026-07-06

## Current state

- **Version:** 0.5.1 (schema version 1)
- **Repos:**
  - Gmail Sorter: https://github.com/Rad-ops/gmail-sorter
  - Local AI Coding Stack: https://github.com/Rad-ops/local-ai-coding-stack
- The historical mailbox cleanup is complete. The tool is now in
  maintenance/relabel mode.

## What changed in 0.5.x

- **Relabel stage** (`--stage relabel --scan full`): reads bodies, removes stale
  `Sorter/*` labels, re-applies the corrected set. Supports `--undo-relabel`,
  `--relabel-run-id` (resume), `--relabel-since-date`, and `--relabel-label`.
- **Per-category confidence** + `--label-confidence` + `--max-labels-per-message`.
- **Body cleaning** (quote/footer stripping) for cleaner categorization.
- **Sender → category profiles** for self-improving re-runs.
- **Word-boundary keyword matching** (fixes substring misclassifications).
- **Package split**: policy data in `src/sorter/`, optional `config/policy.yaml`.
- **Body-feature cache** so re-runs skip expensive `format=full` fetches.

## Suggested next run

```bash
# 1. Re-scan with bodies to refresh labels (uses cached features where available)
python3 src/gmail_sorter.py --scan full --resume --refresh-existing --attachment-details

# 2. Review the dashboard and relabel manifest
#    reports/gmail_sorter_report.html
#    manifests/relabel_manifest.json

# 3. Apply the relabel
python3 src/gmail_sorter.py --stage relabel --scan full --apply --resume \
  --prune-empty-labels

# 4. If something looks wrong, undo by the printed run_id
python3 src/gmail_sorter.py --undo-relabel <run_id> --apply
```

## Validate after edits

```bash
.venv/bin/python -m py_compile src/*.py src/sorter/*.py tests/*.py
.venv/bin/python -m unittest discover -s tests
```

## Code-commenting goal

The code should be readable by a new developer learning how the project works.
Prefer comments that explain intent and safety boundaries:

- why a branch protects mail from archive/trash
- why a Gmail API call uses a specific token or scope
- why generated files stay local
- why local model packets are bounded
- where permanent delete is gated

Do not comment every assignment mechanically.
