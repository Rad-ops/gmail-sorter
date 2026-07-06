# Gmail Sorter

A conservative, dashboard-driven Gmail cleanup and relabeling tool for large or
long-unmanaged mailboxes. It scans, classifies, and reports before any change
is made, then applies label, archive, trash, and relabel stages only when
explicitly requested.

**Version:** `0.5.1` · **Schema version:** 1

Companion local-AI stack: [`Rad-ops/local-ai-coding-stack`](https://github.com/Rad-ops/local-ai-coding-stack)

---

## Why this exists

The target mailbox was unmanaged for more than seven years. The tool is built
around a single principle: **every destructive path has visible reports,
manifests, and explicit flags before Gmail is changed.** Classification is fast
and opinionated; action is slow and gated.

## What it does

| Stage | Purpose | Safety posture |
| --- | --- | --- |
| **Classify** | Scan and categorize mail without changing anything. | Read-only |
| **Label** | Apply `Sorter/<category>` labels. | Lowest risk |
| **Relabel** | Read bodies, remove stale `Sorter/*` labels, re-apply the corrected set. Supports undo and resume. | Reviewable |
| **Archive** | Move low-value bulk mail out of the inbox. | Reviewable |
| **Trash** | Move high-confidence promotional mail to Trash. | Explicit flags required |
| **Rescue audit** | Re-check Trash before permanent deletion, optionally with a local model. | Conservative |

The local-AI review path is intentionally separate from this tool. The sorter
exports bounded review packets; the model never receives Gmail credentials.

## Quick start

```bash
cd sorter
python3 -m pip install -r requirements.txt
# Place Gmail OAuth credentials at secrets/credentials.json
python3 src/gmail_sorter.py --resume
```

Open `reports/gmail_sorter_report.html` to review the classification. Then apply
stages explicitly:

```bash
# Apply labels only
python3 src/gmail_sorter.py --stage label --apply --resume

# Archive bulk mail (requires bulk-mail signals, not just a high ad score)
python3 src/gmail_sorter.py --stage archive --apply --resume

# Trash high-confidence ads (requires explicit acknowledgment)
python3 src/gmail_sorter.py --stage trash --apply \
  --trash-obvious-ads --i-understand-trash --resume
```

## Relabeling from full content

The relabel stage reads each email's body, header, and footer via the Gmail API
(`--scan full`), recomputes labels from the full content, and replaces stale
`Sorter/*` labels with the corrected set. It only ever touches the `Sorter/`
namespace — your user-created and Gmail system labels are never removed.

```bash
# Dry run: scan bodies and preview the relabel diff
python3 src/gmail_sorter.py --stage relabel --scan full --resume --refresh-existing

# Apply the relabel, then prune labels left empty
python3 src/gmail_sorter.py --stage relabel --scan full --apply --resume \
  --prune-empty-labels
```

Review `manifests/relabel_manifest.json` (before → after per message) and the
dashboard's **Relabel Review** section before applying.

### Incremental relabel

Relabel a slice of the mailbox without a full rescan:

```bash
# Only messages on or before a date
python3 src/gmail_sorter.py --stage relabel --scan full --apply --resume \
  --relabel-since-date 2024-01-01

# Only messages currently tagged Sorter/Review
python3 src/gmail_sorter.py --stage relabel --scan full --apply --resume \
  --relabel-label Review
```

### Undo and resume

Every relabel apply records a `run_id` and the previous label set per message in
the action ledger.

```bash
# Undo an entire relabel run (dry run first, then apply)
python3 src/gmail_sorter.py --undo-relabel 20260706T193300
python3 src/gmail_sorter.py --undo-relabel 20260706T193300 --apply

# Resume an interrupted relabel apply (skips already-applied messages)
python3 src/gmail_sorter.py --stage relabel --scan full --apply --resume \
  --relabel-run-id 20260706T193300
```

## Labeling model

- **Word-boundary matching.** Keyword rules use `\b` boundaries, so `exam` does
  not match `example.com` and `class` does not match `classification`.
  Punctuation keywords (e.g. `% off`) are matched as escaped substrings.
- **Per-category confidence.** Each category gets a 0–100 confidence. Categories
  below `--label-confidence` (default 50) are dropped unless protected. A
  `--max-labels-per-message` cap (default 3) prevents label sprawl; protected
  and priority buckets are always kept.
- **Sender → category profiles.** High-confidence and protected decisions are
  accumulated per sender/domain in SQLite. On a re-run, a profile can surface a
  category the subject keywords missed, so the mailbox self-improves pass over
  pass. Disable with `--no-sender-profiles`.
- **Body-aware scanning.** `--scan full` feeds a bounded, cleaned slice of the
  decoded body (quotes and footers stripped) to the classifier. Ad confidence
  is still scored on headers + subject + snippet so a long promotional body
  does not inflate trash scores.
- **Catch-all labels.** `Review` and `Updates` appear on the dashboard but are
  never applied as `Sorter/Review` / `Sorter/Updates` Gmail labels.
- **Primary category.** Each message gets one `primary_category` chosen by a
  protected/priority-first precedence.

## Configuration

Policy data lives in [`src/sorter/policy.py`](src/sorter/policy.py) and can be
overridden without editing code via [`config/policy.yaml`](config/policy.yaml):

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

PyYAML is optional; built-in defaults are used when the file or library is
absent. Allow/block lists live in `config/allowlist.txt` and
`config/blocklist.txt`.

## Project layout

```
sorter/
  src/
    gmail_sorter.py            Runnable core: CLI, scan, decide, apply, reports, dashboard
    sorter/                    Package: policy data and pure logic
      policy.py                Keyword lists, category rules, precedence, defaults
      keywords.py              Word-boundary keyword matcher
      config_loader.py         Optional config/policy.yaml overrides
    trash_rescue_audit.py      Deep re-check of planned Trash before permanent delete
    apply_domain_trash_policy.py  User-approved permanent-delete policy
  config/                      allowlist, blocklist, optional policy.yaml
  secrets/                     Gmail OAuth credentials and tokens (gitignored)
  reports/                     Generated dashboards and CSV/JSON reports (local only)
  manifests/                   Reviewed action manifests (local only)
  data/                        Progress cache, SQLite state, run logs (local only)
  tests/                       unittest suite
  docs/                        Decision log, runbooks, handoff notes
```

Folders marked *local only* are gitignored because they can contain message IDs,
sender domains, snippets, OAuth tokens, and run-specific decisions. GitHub
explains how the sorter works; it does not publish a private mailbox snapshot.

## Safety model

- The default run is classification only. Gmail changes require `--apply`.
- Trash requires `--stage trash --trash-obvious-ads --i-understand-trash`.
- **Protected messages** are never archived or trashed. A message is protected
  when it is allowlisted, important/starred/primary, has real attachments, or
  matches a protected category (immigration, studies, finance, account
  security, health, government/legal, utilities, insurance, receipts/orders,
  work/school).
- **Archive** requires an independent bulk-mail signal (List-Unsubscribe,
  List-Id, one-click unsubscribe, bulk/list precedence, campaign header, Gmail
  Promotions, or a body unsubscribe link) plus `--archive-threshold` — a
  high-scoring one-off message is not pulled from the inbox.
- **Relabel** only touches `Sorter/*` labels. User and system labels are never
  removed. Each apply is recorded in an append-only action ledger and can be
  undone by `run_id`.

### Caps and canaries

| Flag | Stage | Effect |
| --- | --- | --- |
| `--max-trash-total N` | trash | Cap total trash actions |
| `--max-trash-per-domain N` | trash | Cap trash per registered domain |
| `--canary-limit N` | trash | Keep only the first N trash actions on apply |
| `--max-archive-total N` | archive | Cap total archive actions |
| `--max-archive-per-domain N` | archive | Cap archive per registered domain |
| `--archive-canary-limit N` | archive | Keep only the first N archive actions on apply |
| `--archive-min-age-days N` | archive | Keep mail newer than N days in the inbox |
| `--archive-skip-unread` | archive | Never archive unread mail |

## Maintenance mode

After the historical cleanup, scan new mail only:

```bash
python3 src/gmail_sorter.py --maintenance-days 30 --resume --attachment-details
# or from an exact date:
python3 src/gmail_sorter.py --since-date 2026-07-01 --resume
```

## Performance controls

- `--workers N` — parallel read/classification workers (writes stay sequential).
- `--sleep F` — base throttle; auto-increases on quota/rate errors and recovers
  after successes.
- `--http-timeout 120` — Gmail request socket timeout.
- `--scan full` caches compact derived body features (body length, body category
  hits, unsubscribe count) in SQLite so re-runs skip the expensive `format=full`
  fetch. Raw body text is never persisted.
- `--refresh-after-days 7` refreshes cached decisions older than N days when
  `--resume` is used; `--refresh-existing` rescans everything.

## Tests

```bash
python3 -m unittest discover -s tests
```

36 tests cover the classification policy, word-boundary matching, sender
profiles, body-aware scanning, archive gating/caps, the relabel label diff,
undo, resume, and empty-label pruning.

## Documentation

- [Decision log](docs/DECISION-LOG.md) — safety and design choices
- [Overnight local-Qwen runbook](docs/OVERNIGHT-LOCAL-QWEN-RUNBOOK.md)
- [Local AI stack integration](docs/LOCAL-AI-STACK-INTEGRATION.md)
- [Next-run handoff](docs/NEXT-RUN-HANDOFF.md)
- [Changelog](CHANGELOG.md)
