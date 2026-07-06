# HANDOVER

**Purpose:** This file gives a new AI model (or developer) everything needed to
pick up this project and continue working on it. It documents the codebase
architecture, every file's purpose, the design philosophy, how to run the tool,
the AI review workflow, and the current state.

**Last updated:** 2026-07-06  
**Version:** 0.5.1  
**Repository:** https://github.com/Rad-ops/gmail-sorter  
**Schema version:** 1  

---

## 1. What this project is

Gmail Sorter is a conservative, dashboard-driven Gmail cleanup and relabeling
tool for a mailbox that was unmanaged for more than seven years. It scans,
classifies, and reports before any change is made, then applies label, archive,
trash, and relabel stages only when explicitly requested.

The core principle: **classification is fast and opinionated; action is slow
and gated.** Every destructive path has visible reports, manifests, and explicit
flags before Gmail is changed.

## 2. Design philosophy

1. **Scan first, act later.** The default run (`--resume`) is read-only
   classification. No Gmail changes happen without `--apply`.
2. **Protected messages are sacred.** Immigration, studies, finance, health,
   government/legal, account security, real attachments — these are never
   archived or trashed. A protected message can still be labeled, but only
   correctly.
3. **Only the Sorter/ namespace is managed.** The tool never removes
   user-created labels or Gmail system labels (IMPORTANT, STARRED, INBOX used
   only for archive). It only adds/removes `Sorter/<category>` labels.
4. **Privacy-light.** Raw email body text is never persisted. The SQLite
   `message_features` table stores only body length, category keyword names
   that hit, and unsubscribe count. AI review packets contain bounded body
   excerpts (max 1200 chars), not full bodies.
5. **Self-improving.** Sender→category profiles learn from high-confidence
   decisions and fix keyword misses on re-runs. The mailbox gets better
   labeled pass over pass.
6. **AI + code collaboration.** Low-confidence decisions are exported for AI
   review; the AI's suggestions are merged back with safety guards. The code
   is fast and explainable; the AI catches what keywords can't.

## 3. How to run

### Setup

```bash
cd /home/rzangeneh/codebase/sorter
python3 -m pip install -r requirements.txt
```

Place Gmail OAuth credentials at `secrets/credentials.json`. OAuth tokens are
generated on first run and stored in `secrets/`.

### First scan (read-only classification)

```bash
.venv/bin/python src/gmail_sorter.py --resume
```

Outputs: `reports/gmail_sorter_report.html` (dashboard), CSV/JSON reports,
manifests, and SQLite state in `data/`.

### Body-aware scan (for relabeling)

```bash
.venv/bin/python src/gmail_sorter.py --scan full --resume --refresh-existing
```

### Apply labels

```bash
.venv/bin/python src/gmail_sorter.py --stage label --apply --resume
```

### Relabel (read body, remove stale Sorter labels, re-apply corrected set)

```bash
# Dry run
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --resume --refresh-existing

# Apply
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --apply --resume --prune-empty-labels

# Undo a bad relabel run
.venv/bin/python src/gmail_sorter.py --undo-relabel <run_id> --apply

# Resume an interrupted relabel
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --apply --resume --relabel-run-id <run_id>

# Relabel only a slice
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --apply --resume --relabel-label Review
```

### Archive (requires bulk-mail signals)

```bash
.venv/bin/python src/gmail_sorter.py --stage archive --apply --resume \
  --archive-skip-unread --archive-min-age-days 30
```

### Trash (requires explicit acknowledgment)

```bash
.venv/bin/python src/gmail_sorter.py --stage trash --apply \
  --trash-obvious-ads --i-understand-trash --resume
```

### Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

## 4. AI label review workflow

This is the collaborative code+AI labeling pipeline:

### Step 1: Export low-confidence decisions

```bash
.venv/bin/python src/gmail_sorter.py --scan full --resume --refresh-existing --export-ai-review
```

This writes `data/label_review_packets.jsonl`. Each line is a JSON object:

```json
{
  "message_id": "...",
  "sender": "...",
  "sender_email": "...",
  "subject": "...",
  "body_excerpt": "...",          // bounded to 1200 chars, quotes/footers stripped
  "code_categories": ["Shopping"],
  "code_primary_category": "Shopping",
  "code_confidence": {"Shopping": 40},
  "code_reasons": [...],
  "protected": false,
  "ai_label": "",                 // AI fills this
  "ai_confidence": 0,             // AI fills this (0.0-1.0)
  "ai_reason": "",                // AI fills this
  "ai_reviewed": false            // AI sets this to true
}
```

Messages are exported when their top category confidence is below
`--ai-review-threshold` (default 75), OR they landed in a catch-all
(Review/Updates), OR they have conflicting categories. Messages at 100%
confidence are skipped.

### Step 2: AI reviews the file

An AI model (local Qwen, Opencode, Claude, etc.) reads the JSONL file, fills
`ai_label`, `ai_confidence`, `ai_reason`, and sets `ai_reviewed: true`, then
writes the file back. The AI should:
- Read `body_excerpt` and `subject` to understand the email
- Compare its judgment with `code_categories` and `code_confidence`
- Suggest the best single label in `ai_label`
- Set confidence 0.0-1.0 in `ai_confidence`
- Explain in `ai_reason`
- Never suggest removing a protected category

### Step 3: Merge AI suggestions and apply

```bash
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --apply --resume --merge-ai-labels
```

The merge step adjusts decisions where the AI suggests a different label above
`--ai-merge-min-confidence` (default 0.7). Protected status is never removed.
The AI can add a category the code missed but cannot take a protected one away.

## 5. File reference

### Source code

| File | Purpose |
| --- | --- |
| `src/gmail_sorter.py` | **Runnable core** (~2900 lines). CLI, scan, decide, apply, relabel, reports, dashboard. Imports policy/keywords from the `sorter/` package. |
| `src/sorter/__init__.py` | Package init. Re-exports `policy` and `keywords` modules. |
| `src/sorter/policy.py` | **Policy data**: keyword lists (immigration, studies, ad, transactional), `CATEGORY_RULES`, `PROTECTED_CATEGORIES`, `PRIMARY_CATEGORY_PRECEDENCE`, scoring defaults. This is where you edit cleanup rules without touching code. |
| `src/sorter/keywords.py` | **Word-boundary keyword matcher**: `keyword_hits()` with `\b` boundaries, `compile_keywords()`, `regex_hits()`. Fixes the substring bug (`exam` no longer matches `example.com`). |
| `src/sorter/config_loader.py` | Optional `config/policy.yaml` loader. Reads overrides for keyword groups and thresholds. PyYAML optional; falls back to built-in defaults. |
| `src/trash_rescue_audit.py` | Deep re-check of planned Trash before permanent deletion. Re-fetches messages, checks for priority/attachment/durable-record signals, can export bounded packets for local Qwen review. Separate from the main sorter. |
| `src/apply_domain_trash_policy.py` | User-approved permanent-delete policy for obvious-trash domains. Reads rescue-audit JSON, filters by approved domain list, writes delete manifest, permanently deletes with explicit flags. |

### Tests

| File | Purpose |
| --- | --- |
| `tests/test_gmail_sorter.py` | 40 tests: classification policy, word-boundary matching, sender profiles, body-aware scanning, archive gating/caps, relabel label diff, undo, resume, AI review export/merge, confidence/cap behavior, body cleaning. Uses a `FakeGmailService` stub so relabel apply is tested without live Gmail. |
| `tests/test_trash_rescue_audit.py` | Tests for the trash rescue audit. |

### Configuration

| File | Purpose |
| --- | --- |
| `config/allowlist.txt` | One sender email or domain per line. Anything here is protected from archive/trash. |
| `config/blocklist.txt` | One sender email or domain per line. Treated as junk unless protected. |
| `config/policy.yaml` | Optional YAML overrides for keyword groups and thresholds. See file for format. |

### Commands (shell scripts)

| File | Purpose |
| --- | --- |
| `commands/run-overnight-trash-rescue.sh` | Overnight local-Qwen trash rescue audit. |
| `commands/run-overnight-trash-rescue-and-delete-verified.sh` | Same + permanent delete of verified 100%-safe trash. Uses `systemd-inhibit` to prevent sleep. |
| `commands/restore-reviewed-candidates.sh` | Restore rescue candidates after reviewing the audit report. |
| `commands/listen-local-llm-speed.sh` | Monitor local LLM generation speed. |
| `commands/README.md` | Notes on the command scripts. |

### Documentation

| File | Purpose |
| --- | --- |
| `docs/DECISION-LOG.md` | Safety and design decisions, protection model. |
| `docs/NEXT-RUN-HANDOFF.md` | Current state + suggested next-run commands. |
| `docs/LOCAL-AI-STACK-INTEGRATION.md` | How the sorter uses local Qwen3.6 for bounded review. |
| `docs/OVERNIGHT-LOCAL-QWEN-RUNBOOK.md` | Overnight trash rescue runbook. |
| `HANDOVER.md` | This file. |

### Other

| File | Purpose |
| --- | --- |
| `VERSION` | Current version string (e.g. `0.5.1`). |
| `CHANGELOG.md` | Version history with changes per release. |
| `README.md` | Public-facing documentation for GitHub. |
| `GOAL.md` | High-level project goals. |
| `requirements.txt` | Python dependencies: google-api-python-client, google-auth-*, tldextract, PyYAML. |
| `.gitignore` | Excludes secrets, generated reports/manifests/data, pycache, venv. |
| `assets/gmail-sorter-hero.png` | README hero image. |
| `secrets/credentials.json` | Gmail OAuth client secrets (gitignored). |
| `secrets/token_*.json` | OAuth tokens for different scopes (readonly, modify, delete). Gitignored. |

### Generated (gitignored, local only)

| Path | Contents |
| --- | --- |
| `reports/` | HTML dashboards, CSV/JSON reports. Can contain message IDs, sender domains, snippets. |
| `manifests/` | Action manifests (label/archive/trash/relabel). Can contain message IDs and decisions. |
| `data/` | Progress JSON, SQLite state, run logs, AI review packets. Can contain full decision data. |

## 6. Architecture overview

```
                    Gmail API
                       |
                       v
            +-------------------+
            |  gmail_sorter.py  |  <-- CLI entry point
            |                   |
            |  scan_messages()  |  --> fetches message metadata (or full body with --scan full)
            |       |           |
            |       v           |
            |    decide()       |  --> classifies each message
            |       |           |      - score_ad(): promotional likelihood
            |       |           |      - categorize_with_confidence(): per-category 0-100
            |       |           |      - sender profile assist
            |       |           |      - body-aware (clean_body_text strips quotes/footers)
            |       |           |      - confidence floor + cap
            |       |           |
            |       v           |
            |  Decision[]       |  --> list of decisions
            +-------------------+
                    |
        +-----------+-----------+
        |           |           |
        v           v           v
   Reports     Manifests    State DB
   (HTML/CSV)  (JSON)       (SQLite)
        |
        v
   [Human or AI reviews]
        |
        v
   --apply (gated by stage flags)
        |
        +-- label/archive/trash: apply_decisions()
        +-- relabel: apply_relabel() (diff Sorter labels, batchModify)
        +-- undo: undo_relabel() (reverse ledger entries)
        +-- AI merge: merge_ai_labels() (apply AI suggestions)
```

## 7. Key data structures

### `Decision` dataclass (`gmail_sorter.py`)

The central data model. One per message. Key fields:
- `message_id`, `thread_id`, `date`, `sender`, `sender_email`, `sender_domain`
- `categories`: list of assigned category names
- `primary_category`: single strongest category (by precedence)
- `category_confidence`: `{category: 0-100}` per-category confidence
- `ad_confidence`: 0-100 promotional likelihood
- `reasons`, `negative_reasons`: why each decision was made
- `planned_actions`: `["label:Finance", "archive"]` etc.
- `archive_reason`: evidence string for archive decisions
- `protected`: bool — safety gate
- `body_len`, `body_category_hits`: body-derived features
- `schema_version`: for future migrations

### SQLite state (`data/gmail_sorter_state.sqlite`)

Tables:
- `messages`: latest decision per message (full JSON in `decision_json`)
- `action_ledger`: append-only record of every Gmail write (label/archive/trash/relabel)
- `domain_review`: per-domain review state
- `sender_profile`: learned sender→category weights
- `message_features`: cached body features (body length, category hits, unsubscribe count)

## 8. Labeling model

### Keyword matching
- `keyword_hits()` in `sorter/keywords.py` uses `\b` word boundaries for word-like keywords and escaped substrings for punctuation.
- This fixes the substring bug: `exam` no longer matches `example.com`, `class` no longer matches `classification`.

### Category confidence scoring (`categorize_with_confidence`)
- Subject keyword hits: **30 each** (strong signal — the sender chose these words)
- Body keyword hits: **20 each** (weaker — body text is longer and noisier)
- Sender/domain hits: **15 each**
- Keyword family capped at 75
- Gmail `CATEGORY_*` label boost: **+30** (the mail transport's own classification)
- Sender profile boost: up to **+25**
- Capped at 100 total

### Label application
- Categories below `--label-confidence` (default 50) are dropped unless protected/priority
- `--max-labels-per-message` (default 3) caps applied labels; protected buckets always kept
- Catch-all `Review`/`Updates` never applied as Gmail labels
- `primary_category` chosen by protected/priority-first precedence

### Sender profiles
- Accumulated from high-confidence (≥65) and protected decisions
- Stored in SQLite `sender_profile` table
- Sender-level hits outweigh domain-level (3:1 weight ratio)
- On re-runs, can add a category the keywords missed entirely

### Body-aware scanning
- `--scan full` fetches `format=full` and decodes body text
- `clean_body_text()` strips quoted reply chains, forwarded blocks, footer/signature lines
- Body fed to `categorize_with_confidence()` for richer classification
- Ad confidence still scored on headers+subject+snippet only (no inflation)
- Body features cached in SQLite so re-runs skip the expensive fetch

## 9. Safety model

### Protected messages
Never archived or trashed when:
- Allowlisted (config/allowlist.txt)
- Important/starred/primary Gmail labels
- Has real attachments (PDFs/documents — inline images are not "real")
- Matches a protected category: immigration, studies, finance, account security, health, government/legal, utilities, insurance, receipts/orders, work/school

### Archive gating
- Requires an independent bulk-mail signal (List-Unsubscribe, List-Id, one-click unsubscribe, bulk/list precedence, campaign header, Gmail Promotions, or body unsubscribe link)
- Plus `--archive-threshold` confidence
- Guards: `--archive-skip-unread`, `--archive-min-age-days`
- Caps: `--max-archive-total`, `--max-archive-per-domain`, `--archive-canary-limit`

### Trash gating
- Requires `--stage trash --trash-obvious-ads --i-understand-trash`
- Only for perfect ad matches or ad_confidence >= trash_threshold
- Mixed-thread protection: if a thread has messages outside the plan, trash is stripped
- Caps: `--max-trash-total`, `--max-trash-per-domain`, `--canary-limit`

### Relabel safety
- Only touches `Sorter/*` labels — user/system labels never removed
- Each apply recorded in action ledger with previous labels + run_id
- `--undo-relabel <run_id>` reverses any run
- `--relabel-run-id <run_id>` resumes interrupted applies

### AI merge safety
- AI can add a label but cannot remove a protected category
- Only applies when AI confidence >= 0.7
- All overrides recorded in `reasons` as `ai_override:<category>:<conf>`

## 10. Current state and next steps

- **Version:** 0.5.1 (schema version 1)
- **Tests:** 40 passing
- **Branch:** `v0.5-relabel` (PR #2 open)
- **Historical cleanup:** complete (trash applied, rescue audited, verified delete done)
- **Current mode:** maintenance + relabeling

### Suggested next work
1. Run a full body-aware rescan (`--scan full --refresh-existing`) on the mailbox
2. Export AI review packets (`--export-ai-review`) and have a model review them
3. Merge AI labels and apply relabel (`--merge-ai-labels --stage relabel --apply`)
4. Consider moving `CATEGORY_RULES` into `config/policy.yaml` for config-driven rules
5. Consider GitHub Actions CI for automated test gating

## 11. The companion AI stack

Repository: https://github.com/Rad-ops/local-ai-coding-stack

- **Qwen3.6-35B-A3B-MTP**: mailbox review (bounded packets, no Gmail access)
- **DeepSeek-R1-Distill-Qwen-32B**: reasoning fallback
- **Gemma 4 26B MoE**: planner/architect (outside mailbox pipeline)

Local LLM server: `http://127.0.0.1:8080/v1` (systemd service `local-llm`)
Profile switcher: `llm-switch` (preferred profile: `qwen36`)

The local AI path is **separate** from the main sorter. The sorter exports
bounded review packets; the model never receives Gmail credentials.
