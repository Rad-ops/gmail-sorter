# Gmail Sorter

A conservative, dashboard-driven Gmail cleanup and relabeling tool for large or
long-unmanaged mailboxes. It scans, classifies, and reports before any change
is made, then applies label, archive, trash, and relabel stages only when
explicitly requested.

**Version:** `0.8.0` · **Schema version:** 4

Companion local-AI stack: [`Rad-ops/local-ai-coding-stack`](https://github.com/Rad-ops/local-ai-coding-stack)

---

## Overview

The target mailbox was unmanaged for more than seven years. Rather than bulk-delete or trust Gmail's auto-categorization, this tool builds an auditable paper trail: scan first, classify, report, then act.

The core principle is simple: **classification is fast and opinionated; action is slow and gated.** No Gmail change happens without `--apply`. No trash happens without explicit acknowledgment. Protected messages are never archived or trashed. An AI review pipeline catches what keyword rules can't.

## Quick start

```bash
cd sorter
python3 -m pip install -r requirements.txt
```

Place Gmail OAuth credentials at `secrets/credentials.json`, then run a read-only classification scan:

```bash
.venv/bin/python src/gmail_sorter.py --resume
```

Open `reports/gmail_sorter_report.html` to review. Then apply stages explicitly (see below).

## How it works

| Stage | Purpose | Safety posture |
| --- | --- | --- |
| **Classify** | Scan and categorize mail without changing anything. | Read-only |
| **Label** | Apply `Sorter/<category>` labels. | Lowest risk |
| **Relabel** | Read bodies, remove stale `Sorter/*` labels, re-apply the corrected set. Supports undo and resume. | Reviewable |
| **Archive** | Move low-value bulk mail out of the inbox. | Reviewable |
| **Trash** | Move high-confidence promotional mail to Trash. | Explicit flags required |
| **Rescue audit** | Re-check Trash before permanent deletion, optionally with a local model. | Conservative |

## Stages

### Classify (read-only)

```bash
.venv/bin/python src/gmail_sorter.py --resume
```

Scans, classifies, and writes reports/dashboard. No Gmail changes.

### Label

```bash
.venv/bin/python src/gmail_sorter.py --stage label --apply --resume
```

Applies `Sorter/<category>` labels. Only meaningful categories are applied; catch-all buckets (`Review`, `Updates`) are kept for the dashboard but never tagged.

### Relabel

Reads each email's body, header, and footer via the Gmail API (`--scan full`), recomputes labels from the full content, and replaces stale `Sorter/*` labels with the corrected set. Only ever touches the `Sorter/` namespace.

```bash
# Dry run: scan bodies and preview the relabel diff
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --resume --refresh-existing

# Apply
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --apply --resume \
  --prune-empty-labels

# Undo a bad relabel run
.venv/bin/python src/gmail_sorter.py --undo-relabel <run_id> --apply

# Resume an interrupted relabel
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --apply --resume \
  --relabel-run-id <run_id>

# Relabel only a slice
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --apply --resume \
  --relabel-label Review
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --apply --resume \
  --relabel-since-date 2024-01-01
```

### Archive

```bash
.venv/bin/python src/gmail_sorter.py --stage archive --apply --resume \
  --archive-skip-unread --archive-min-age-days 30
```

Requires an independent bulk-mail signal (not just a high ad score).

### Trash

```bash
.venv/bin/python src/gmail_sorter.py --stage trash --apply \
  --trash-obvious-ads --i-understand-trash --resume
```

### Maintenance

```bash
.venv/bin/python src/gmail_sorter.py --maintenance-days 30 --resume --attachment-details
.venv/bin/python src/gmail_sorter.py --since-date 2026-07-01 --resume
```

## AI-assisted label review

The code classifies with keyword rules + sender profiles + confidence scoring. That is fast and explainable, but it cannot understand context, intent, or nuance the way a language model can. So low-confidence decisions are exported as bounded review packets for an AI to inspect, suggest corrections, and write back.

### Step 1: Export

```bash
.venv/bin/python src/gmail_sorter.py --scan full --resume --refresh-existing --export-ai-review
```

Writes `data/label_review_packets.jsonl`. Each line is a JSON object with sender, subject, bounded body excerpt, the code's categories/confidence/reasons, and empty `ai_*` fields. Messages are exported when their top confidence is below `--ai-review-threshold` (default 75), or they landed in a catch-all, or they have conflicting categories. 100%-confidence messages are skipped.

### Step 2: AI reviews

An AI model reads the JSONL, fills `ai_label`, `ai_confidence` (0.0–1.0), `ai_reason`, sets `ai_reviewed: true`, and writes the file back. The AI should never suggest removing a protected category.

### Step 3: Merge and apply

```bash
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --apply --resume --merge-ai-labels
```

The merge step adjusts decisions where the AI suggests a different label above `--ai-merge-min-confidence` (default 0.7). Protected status is never removed; the AI can add a label but cannot take a protected one away.

## Labeling model

- **Word-boundary matching.** `exam` does not match `example.com`; `class` does not match `classification`. Punctuation keywords match as escaped substrings.
- **Per-category confidence.** Each category gets a 0–100 score. Subject keyword hits weight 30 each (the sender chose those words), body hits 20, sender/domain hits 15. Categories below `--label-confidence` (default 50) are dropped unless protected. `--max-labels-per-message` (default 3) caps applied labels.
- **Sender → category profiles.** High-confidence decisions are accumulated per sender/domain in SQLite. On a re-run, a profile can surface a category the subject keywords missed — the mailbox self-improves pass over pass.
- **Body-aware scanning.** `--scan full` feeds a bounded, cleaned slice of the decoded body (quotes and footers stripped) to the classifier. Ad confidence is still scored on headers + subject + snippet so a long promotional body does not inflate trash scores. Body features are cached in SQLite so re-runs skip the expensive fetch.
- **Embedding pre-classifier.** `--use-embeddings` computes a dense embedding for each message and compares it to per-category centroid vectors learned from past high-confidence decisions. The final confidence is `max(keyword_score, embedding_similarity * 100)` — the keyword rules provide the explainable floor, the embedding provides the semantic ceiling. This catches semantic matches the lexical rules miss (e.g. a bank statement with no "bank" keyword). Uses the local LLM server's `/v1/embeddings` endpoint or sentence-transformers; falls back to keyword-only when unavailable.
- **Multi-language keyword overlays.** `sorter/lang.py` picks `en|fr|fa|other` per message. `config/policy.fr.yaml` and `config/policy.fa.yaml` add French and Farsi keywords for IRCC, finance, health, government, utilities, and security. The detector is *only* used to pick the keyword overlay; it never blocks or moves mail.
- **Better HTML body extraction.** Each email part is decoded with the right `Content-Transfer-Encoding` (base64, quoted-printable, 7bit, 8bit). HTML parts are converted to structured text via an `HTMLParser`-based converter that preserves tables as tab-separated rows and skips `<style>`/`<script>` blocks. Receipts and non-English QP-encoded bodies are now decoded correctly. Opt-in via `--use-html-body` (default on).
- **Per-keyword learned weights.** A 6-feature logistic regression per category fits on the labeled data in the SQLite `messages` table. The result is persisted to `data/learned_weights.json` and consulted by `decide()` via `max(keyword, learned)`. Opt-in via `--use-learned-weights`.
- **Thread-level conversation modeling.** A new `thread_features` table carries one row per (thread_id, message_count, distinct_senders, top_category_share, has_attachment_count, has_unsubscribe_count, date_span_days, protected_fraction). The thread model returns a 0-15 confidence boost for the thread's top category. Opt-in via `--use-thread-modeling` (default on).
- **Sender reputation as a first-class signal.** A new `sender_reputation` table carries one row per (sender, domain, total_messages, avg_ad_confidence, protected_fraction, ad_fraction, first/last_seen, reputation_score). The score is `100 * (1 - ad_fraction) * log(1 + N) / 5`, clamped to 0-100. The dashboard surfaces suggested blocklist candidates. Opt-in via `--use-sender-reputation` (default on).
- **Gmail History API incremental scan.** A new `state_meta` table stores the last `historyId` we've processed. `--since-history-id {auto,reset,<id>}` enables the incremental path. `commands/run-maintenance.sh` runs the weekly cadence via a systemd user timer.
- **Sender profile time-decay + diversity.** New `--sender-profile-half-life-days` (default 180). The `sender_profile` key now includes the category so a single sender can have one row per category. `category_diversity` is refreshed on every write so the dashboard can surface noisy senders.
- **Catch-all labels.** `Review` and `Updates` appear on the dashboard but are never applied as Gmail labels.
- **Primary category.** Each message gets one `primary_category` chosen by a protected/priority-first precedence.

## Safety model

- The default run is classification only. Gmail changes require `--apply`.
- Trash requires `--stage trash --trash-obvious-ads --i-understand-trash`.
- **Protected messages** are never archived or trashed. A message is protected when it is allowlisted, important/starred/primary, has real attachments, or matches a protected category (immigration, studies, finance, account security, health, government/legal, utilities, insurance, receipts/orders, work/school).
- **Archive** requires an independent bulk-mail signal (List-Unsubscribe, List-Id, one-click unsubscribe, bulk/list precedence, campaign header, Gmail Promotions, or a body unsubscribe link) plus `--archive-threshold`.
- **Relabel** only touches `Sorter/*` labels. User and system labels are never removed. Each apply is recorded in an append-only action ledger and can be undone by `run_id`.
- **AI merge** never removes protected categories. Only applies when AI confidence ≥ 0.7.

## Configuration

Policy data lives in [`src/sorter/policy.py`](src/sorter/policy.py) and can be overridden without editing code via [`config/policy.yaml`](config/policy.yaml):

```yaml
immigration_keywords:
  - immigration
  - ircc
  - "work permit"

thresholds:
  ad_threshold: 65
  archive_threshold: 65
  trash_threshold: 90
  pre_2020_trash_threshold: 75
```

Per-language overlays live next to `policy.yaml` as `policy.fr.yaml` and `policy.fa.yaml`. The English overlay is the regular `policy.yaml`; the FR/FA overlays are picked automatically when the language detector returns `fr` or `fa` on a message. The overlays are additive by default; a category can be replaced with `replace: true` on a per-category basis (opt-in).

PyYAML is optional; built-in defaults are used when the file or library is absent. Allow/block lists live in `config/allowlist.txt` and `config/blocklist.txt`.

## CLI reference

### Scan

| Flag | Effect |
| --- | --- |
| `--scan {metadata,full}` | metadata = headers+snippet (fast); full = also read decoded body |
| `--workers N` | Parallel read/classification workers |
| `--sleep F` | Base throttle; auto-increases on quota errors |
| `--http-timeout 120` | Gmail request socket timeout |
| `--resume` | Reuse and update the progress JSON |
| `--refresh-existing` | Rescan all cached decisions |
| `--refresh-after-days 7` | Refresh cached decisions older than N days |

### Labeling

| Flag | Effect |
| --- | --- |
| `--label-confidence 50` | Minimum per-category confidence to apply a label |
| `--max-labels-per-message 3` | Cap applied Sorter labels per message |
| `--use-sender-profiles` / `--no-sender-profiles` | Toggle sender-profile assist |
| `--use-thread-aware` | Propagate thread's dominant category to catch-all replies |
| `--use-embeddings` | Enable embedding-based semantic classification (hybrid with keywords) |
| `--embedding-endpoint URL` | OpenAI-compatible /v1/embeddings endpoint |
| `--embedding-st-model NAME` | sentence-transformers model (offline, if installed) |
| `--sender-profile-half-life-days N` | v0.7: half-life in days for sender-profile time decay. 0 disables. |
| `--use-html-body` (default on) | v0.8: better HTML body extraction (script/style stripped, tables as tab-separated rows, QP-decoded bodies). |
| `--use-learned-weights` | v0.8: replace the hand-tuned keyword weights with weights learned from the labeled data in the SQLite messages table. |
| `--learned-weights-file PATH` | v0.8: path to the learned-weights JSON file (default `data/learned_weights.json`). |
| `--use-thread-modeling` (default on) | v0.8: thread-level conversation modeling. Boosts a category's confidence by up to 15 points based on the thread's message count and top-category share. |
| `--use-sender-reputation` (default on) | v0.8: first-class sender reputation signal. High-reputation senders get -15 ad confidence, low-reputation senders get +10. The dashboard surfaces suggested blocklist candidates. |
| `--since-history-id ID` | v0.8: incremental scan via the Gmail History API. Pass a numeric historyId, `auto` to use the stored last_history_id, `reset` to force a full re-scan, or empty to disable. |

### Archive

| Flag | Effect |
| --- | --- |
| `--archive-threshold 65` | Minimum ad confidence for archive |
| `--archive-min-age-days N` | Keep mail newer than N days in the inbox |
| `--archive-skip-unread` | Never archive unread mail |
| `--max-archive-total N` | Cap total archive actions |
| `--max-archive-per-domain N` | Cap archive per registered domain |
| `--archive-canary-limit N` | Keep only the first N archive actions on apply |

### Trash

| Flag | Effect |
| --- | --- |
| `--trash-obvious-ads` | Allow trash actions during trash stage |
| `--i-understand-trash` | Required acknowledgment |
| `--max-trash-total N` | Cap total trash actions |
| `--max-trash-per-domain N` | Cap trash per registered domain |
| `--canary-limit N` | Keep only the first N trash actions on apply |

### Relabel

| Flag | Effect |
| --- | --- |
| `--prune-empty-labels` | Delete empty `Sorter/*` labels after apply |
| `--relabel-since-date YYYY-MM-DD` | Restrict to messages on or before a date |
| `--relabel-label NAME` | Restrict to messages with a current Sorter label |
| `--undo-relabel RUN_ID` | Reverse a relabel run |
| `--relabel-run-id RUN_ID` | Resume an interrupted apply |

### AI review

| Flag | Effect |
| --- | --- |
| `--export-ai-review` | Export low-confidence decisions as JSONL |
| `--ai-review-threshold 75` | Export decisions below this top confidence |
| `--ai-review-file PATH` | Path to the review JSONL |
| `--merge-ai-labels` | Merge AI-reviewed labels before apply |
| `--ai-merge-min-confidence 0.7` | Minimum AI confidence to add a label |
| `--ai-merge-min-removal-confidence 0.85` | v0.7: minimum AI confidence to REMOVE a non-protected label the code assigned |
| `--no-ai-learning` | v0.7: disable the active-learning pass that pushes AI-verified decisions into sender_profile and category centroids |

## Project structure

```
sorter/
  src/
    gmail_sorter.py              Runnable core: CLI, scan, decide, apply, reports
    sorter/                      Package: policy data and pure logic
      policy.py                  Keyword lists, category rules, precedence, defaults
      keywords.py                Word-boundary keyword matcher
      config_loader.py           Optional config/policy.yaml overrides
    trash_rescue_audit.py        Deep re-check of planned Trash before permanent delete
    apply_domain_trash_policy.py User-approved permanent-delete policy
  config/                        allowlist, blocklist, optional policy.yaml
  secrets/                       Gmail OAuth credentials and tokens (gitignored)
  reports/                       Generated dashboards and CSV/JSON reports (local only)
  manifests/                     Reviewed action manifests (local only)
  data/                          Progress cache, SQLite state, run logs, AI review packets (local only)
  tests/                         unittest suite
  docs/                          Decision log, runbooks, handoff notes
  HANDOVER.md                    Comprehensive handover for model/developer handoff
```

Folders marked *local only* are gitignored because they can contain message IDs, sender domains, snippets, OAuth tokens, and run-specific decisions.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

304 tests cover the classification policy, word-boundary matching, sender profiles, body-aware scanning, archive gating/caps, the relabel label diff, undo, resume, AI review export/merge, AI active learning, confidence/cap behavior, body cleaning, thread-aware labeling, embedding-based semantic classification, language detection, per-language keyword overlays, sender profile time decay, schema migrations, per-keyword learned weights, thread-level conversation modeling, sender reputation, Gmail History API incremental scan, and HTML body extraction.

## Documentation

- [HANDOVER.md](HANDOVER.md) — comprehensive handover for model/developer handoff
- [Decision log](docs/DECISION-LOG.md) — safety and design choices
- [Next-run handoff](docs/NEXT-RUN-HANDOFF.md) — current state and suggested commands
- [Local AI stack integration](docs/LOCAL-AI-STACK-INTEGRATION.md)
- [Overnight local-Qwen runbook](docs/OVERNIGHT-LOCAL-QWEN-RUNBOOK.md)
- [Changelog](CHANGELOG.md)
