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

## 12. How the script works (for the next agent/model)

This section explains the runtime flow end-to-end so a new model can reason
about what the code does and why it does it that way.

### The scan → decide → report → apply pipeline

```
1. list_message_ids()        Query Gmail for message IDs matching --query
2. scan_messages()           Fetch each message (metadata or full body) in parallel
   └─ decide()               Classify one message:
      ├─ score_ad()          Score promotional likelihood (0-100) from headers,
      │                      subject, snippet, bulk-mail headers, sender localpart
      ├─ categorize_with_confidence()
      │                      Score each category 0-100 from:
      │                        - subject keyword hits (30 each — sender chose them)
      │                        - body keyword hits (20 each — noisier)
      │                        - sender/domain keyword hits (15 each)
      │                        - Gmail CATEGORY_* label boost (+30)
      │                        - sender-profile boost (+25 max)
      │                      Cap at 75 for the keyword family, 100 total.
      ├─ Shopping suppressed  If Ads Promotions >= 65, Shopping is dropped
      ├─ Confidence floor      Categories below --label-confidence dropped
      │                        (protected/priority always kept)
      ├─ Label cap             --max-labels-per-message caps applied labels
      ├─ Thread-aware          If catch-all (Review) and --use-thread-aware,
      │                        inherit thread's dominant category at 55
      ├─ Protected check       Allowlist, real attachments, protected categories,
      │                        IMPORTANT/STARRED/PRIMARY → protected=True
      ├─ Archive gating        Requires bulk-mail signal + threshold
      └─ Trash gating          Perfect ad match or ad_confidence >= trash_threshold
3. save_progress()           Write decisions to JSON progress + SQLite state
4. write_dashboard()         HTML dashboard with review queues, tables, manifests
5. [optional] export_ai_review_packets()
                             Write low-confidence decisions as JSONL for AI review
6. [optional] merge_ai_labels()
                             Read AI-reviewed JSONL, adjust decisions where
                             AI suggests a different label above 0.7 confidence
                             (protected status never removed)
7. [optional] --apply        apply_decisions() or apply_relabel() — gated by
                             stage flags, recorded in action_ledger
```

### Why keyword rules, not embeddings or a model?

The codebase deliberately uses **keyword rules + confidence scoring** as the
primary classifier, not embeddings or a neural model. The reasons:

1. **Explainability.** Every decision has a `reasons` list
   (`subject:bank`, `sender_profile:Finance:9`, `thread_inherited:Finance`)
   that the dashboard shows. A reviewer can see *why* a message was labeled
   Finance. An embedding classifier is a black box — you can't explain why
   "your statement is ready" is Finance.

2. **Determinism.** The same input always produces the same output. A model
   can be non-deterministic between runs, which makes auditing impossible.

3. **Cost.** Keyword rules run in microseconds on the local machine. A model
   inference per message would add seconds and GPU/API cost across tens of
   thousands of messages.

4. **Safety.** The sorter's job is to *not* destroy important mail. A keyword
   rule that says "immigration/IRCC/visa → protected" is a hard, auditable
   gate. A model might "forget" to protect a visa email because the embedding
   was close to a promo email.

### Where keyword rules fall short (and what the AI review pipeline does about it)

Keyword rules can't understand:
- **Context.** "Your appointment has been rescheduled" from a clinic is Health;
  from a recruiter is Job Search. The word "appointment" matches both.
- **Intent.** A promo email titled "Your order is ready" is actually an ad,
  not a receipt. The word "order" matches Receipts Orders.
- **Negation.** "Do not reset your password" contains "password" and "reset"
  but is not a security alert.
- **Sender ambiguity.** `no-reply@accounts.google.com` sends both security
  alerts and promotional newsletters. The domain alone can't disambiguate.

The AI review pipeline (Section 4) is the bridge: the code's keyword rules
make a fast, explainable first pass; low-confidence decisions are exported
with bounded body excerpts and context; an AI model that *can* understand
context reviews them and suggests corrections; the script merges both opinions
before applying. The code never gives the AI Gmail access — only bounded
packets.

### Key invariants a new model must preserve

1. **Protected messages are never archived or trashed.** This is the single
   most important safety rule. Any change to `decide()` or `apply_*()` must
   preserve it.
2. **Only `Sorter/*` labels are managed.** Never remove user-created or Gmail
   system labels in relabel.
3. **AI merge never removes a protected category.** The AI can add a label but
   cannot take a protected one away.
4. **Raw body text is never persisted.** Only bounded excerpts (1200 chars,
   quotes/footers stripped) go into AI packets; only body_len + category hit
   names go into SQLite.
5. **Every Gmail write is recorded in the action_ledger.** Every label/archive/
   trash/relabel call appends a row so it can be audited and undone.
6. **`--apply` is always required for Gmail changes.** The default run is
   read-only.

## 13. Architectural improvement suggestions (from the current model's POV)

These are suggestions for the next model/reviewer to evaluate. They reflect
the current model's assessment of where the architecture is weakest and what
would move it to the next level. The user will review these alongside another
model's input before deciding what to run.

### A. Move from keyword matching to context-aware classification

**Current state:** Classification relies on keyword rules
(`CATEGORY_RULES` in `policy.py`) — lists of words that, when found in the
subject/body/sender, trigger a category at a confidence score. This is fast
and explainable but fundamentally **lexical, not semantic**. It cannot
understand context, intent, or the relationship between words.

**The problem:** Real emails don't contain neat keywords. A bank statement
says "Your January statement is now available" — no "bank" or "finance"
keyword, but it's clearly Finance. A clinic email says "See you Tuesday at 3"
— no "appointment" keyword, but it's clearly Health. The keyword rules miss
these, and the sender profile + thread-aware fixes only paper over the gap.

### A. Embedding pre-classifier (IMPLEMENTED in v0.6.0)

**Status:** Done. `--use-embeddings` computes a dense embedding for each
message and compares it to per-category centroid vectors learned from past
high-confidence decisions. The final confidence is
`max(keyword_score, embedding_similarity * 100)`. Two backends: HTTP endpoint
(local LLM server's `/v1/embeddings`) or sentence-transformers. Falls back to
keyword-only when unavailable. Centroids stored in `category_centroid` SQLite
table. New module: `src/sorter/embeddings.py`. 6 regression tests added.

**What was built:**
1. `src/sorter/embeddings.py` — `HttpEmbeddingBackend`, `SentenceTransformerBackend`, `compute_embedding_scores()`, `cosine_similarity()` (pure Python, no numpy), `average_vectors()`, `create_embedding_backend()`.
2. `category_centroid` SQLite table — stores per-category average embedding vectors.
3. `load_category_centroids()` / `update_category_centroids()` — load before scan, update after scan from decisions at or above `--embedding-confidence-floor` (default 70).
4. Hybrid scoring in `decide()` — `max(keyword_confidence, embedding_similarity * 100)` per category. Reasons record `embedding_boost:<cat>:<sim>` when the embedding wins.
5. CLI flags: `--use-embeddings`, `--embedding-endpoint`, `--embedding-model`, `--embedding-st-model`, `--embedding-confidence-floor`.

**What remains for the next model:**
- Run a first scan with `--use-embeddings` to learn initial centroids, then a second scan to benefit from them.
- Consider adding a confidence-calibration curve (item E) to validate that the embedding similarity scores are well-calibrated.
- The `sentence-transformers` backend requires PyTorch; the HTTP backend is lighter and preferred when the local LLM server is running.

### B. Replace per-keyword scoring with a lightweight trained classifier

**Current state:** Each keyword hit adds a fixed weight (subject: 30, body:
20, sender: 15). These weights were hand-tuned by guessing.

**Proposed:** Train a small logistic-regression or gradient-boosted classifier
on the existing labeled data (the SQLite `messages` table has thousands of
decisions with `categories` and `category_confidence`). Features:
- keyword hit counts per category (current input)
- sender-domain one-hot (or embedding)
- Gmail CATEGORY_* labels
- hour-of-day / day-of-week (marketing mail is sent at specific times)
- list-unsubscribe / precedence headers

This would replace the hand-tuned weights with learned weights and likely
improve accuracy significantly with zero runtime cost (a logistic regression
is microseconds). The model file is a few KB and can be versioned in Git.

**Risk:** Requires a training step. The user would need to run
`python3 src/train_classifier.py` after labeling enough mail. But the
`messages` table already has the labeled data from the first cleanup pass.

### C. Thread-level conversation modeling

**Current state:** `--use-thread-aware` propagates the thread's *dominant*
category to catch-all replies. But it's a simple plurality vote — it doesn't
model the *conversation* (who said what, reply chains, forwarded context).

**Proposed:** Build a thread-level feature vector per thread:
- number of messages, span of dates, distinct senders
- category distribution across the thread
- presence of attachments, unsubscribe headers, promotional signals

This thread context feeds into `decide()` as an additional signal: a reply in
a 10-message Finance thread with attachments gets a much stronger Finance
boost than a reply in a 2-message thread with mixed categories. This is more
principled than the current dominant-category plurality.

### D. Sender reputation as a first-class signal

**Current state:** `sender_profile` learns "this sender was labeled Finance 9
times." But there's no notion of *reputation* — how much mail this sender
sends, what fraction is promotional, whether they're on a blocklist, etc.

**Proposed:** A `sender_reputation` table:
- `total_messages`, `avg_ad_confidence`, `protected_fraction`
- `categories_distribution` (JSON)
- `first_seen`, `last_seen`
- `reputation_score` (0–100, derived)

This would:
- Auto-suggest blocklist entries in the dashboard (domain with 500 messages,
  95% ad confidence → "suggest blocklist")
- Provide a stronger prior for new messages from known senders
- Surface "noisy senders" that should be unsubscribed

### E. Confidence calibration and golden-set testing

**Current state:** Confidence scores (30/20/15 for subject/body/sender) are
hand-tuned. There's no way to know if a score of 50 actually means "50% likely
correct."

**Proposed:**
1. **Golden set.** Manually label 100–200 messages with the "correct"
   category. Store them in `tests/golden_set.jsonl`.
2. **Calibration script.** Run the classifier on the golden set and produce a
   calibration curve: for each confidence bucket (0–10, 10–20, ...), what
   fraction are actually correct? Apply a Platt-scaling or isotonic regression
   to map raw scores to calibrated probabilities.
3. **Regression test.** The golden set becomes a test: if accuracy drops below
   a threshold, the test fails. This catches regressions when keywords or
   weights change.

This turns "confidence" from a guess into a measurable, tunable quantity.

### F. Full module split of the core

**Current state:** `gmail_sorter.py` is still ~3100 lines. Policy data and
keyword matching are in the `sorter/` package, but the Gmail I/O, decide,
apply, relabel, reports, and dashboard are all in one file.

**Proposed:** Split into `sorter/{gmail_client, scoring, classify, features,
relabel, reports, dashboard, apply, ai_review, cli}.py`. `gmail_sorter.py`
becomes a thin shim. This was deferred to keep the live tool safe, but the
file is now large enough that the split would meaningfully improve
maintainability.

### Summary of priorities (my recommendation)

| Priority | Improvement | Effort | Impact |
| --- | --- | --- | --- |
| 1 | ~~A. Embedding pre-classifier (hybrid)~~ ✅ Done in v0.6.0 | — | — |
| 2 | E. Confidence calibration + golden set | Medium | High — turns confidence from a guess into a metric |
| 3 | D. Sender reputation table | Medium | Medium — auto-suggests blocklist, stronger priors |
| 4 | B. Trained classifier on labeled data | Medium | Medium — replaces hand-tuned weights |
| 5 | C. Thread-level conversation modeling | Medium | Medium — more principled than plurality vote |
| 6 | F. Full module split | Low | Low — maintainability, not accuracy |

**A (embedding pre-classifier)** is now implemented in v0.6.0. It addresses
the root cause the user identified: "we rely heavily on keywords and domains,
shouldn't we rely more on context?" An embedding model captures context; the
keyword rules stay as the explainable floor; the AI review pipeline catches
the rest. The hybrid is the architecture that makes the sorter genuinely smart
without sacrificing safety or explainability.

The next model should evaluate the **remaining** items (B–F) and decide which
to implement next. My recommendation for the next priority is **E (confidence
calibration + golden set)** because it turns the embedding similarity scores
into measurable, tunable probabilities — without calibration, we can't know
if a similarity of 0.7 actually means "70% likely correct."
