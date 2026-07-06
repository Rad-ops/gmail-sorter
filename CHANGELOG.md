# Changelog

## 0.6.0 - 2026-07-06

### 🧠 Embedding Pre-Classifier (Hybrid Keyword + Semantic)

- New `--use-embeddings` flag enables an optional semantic classification layer. Each message's subject + body excerpt is embedded into a dense vector and compared to per-category centroid vectors learned from past high-confidence decisions.
- The final category confidence is `max(keyword_score, embedding_similarity * 100)` — the keyword rules provide the explainable floor, the embedding provides the semantic ceiling. This catches semantic matches the lexical rules miss (e.g. a bank statement with no "bank" keyword still embeds close to the Finance centroid).
- Two backends: HTTP endpoint (local LLM server's `/v1/embeddings`) or sentence-transformers (offline). Falls back to keyword-only when neither is available.
- Per-category centroids are stored in a new `category_centroid` SQLite table and updated after each scan from decisions at or above `--embedding-confidence-floor` (default 70). A category needs at least 3 high-confidence messages before a centroid is created.
- All vector math is pure Python (no numpy dependency). Embeddings are not reversible — they do not contain readable email content.
- New module: `src/sorter/embeddings.py` (embedding client, centroid management, cosine similarity).
- New CLI flags: `--use-embeddings`, `--embedding-endpoint`, `--embedding-model`, `--embedding-st-model`, `--embedding-confidence-floor`.

### 📚 Documentation

- README updated with the embedding pre-classifier in the labeling model section and CLI reference.
- HANDOVER.md Section 13 (architectural suggestions) updated: item A (embedding pre-classifier) is now marked as **implemented**.

### 🧪 Tests

- 51 tests passing. Added 6 embedding regression tests: cosine similarity math, embedding scores with mock backend, embedding boost on keyword miss, embedding never lowers keyword score, fallback to keyword-only, and empty-backend handling.

## 0.5.2 - 2026-07-06

### 🐛 Bug Fixes

- **B1: Keyword overlaps.** 8 keywords appeared in 2 categories simultaneously (e.g. "study permit" in both Immigration and Studies, "university" in both Studies and Work School). Now each keyword belongs to exactly one category, with the more specific/protected category winning. Verified: zero overlaps across all `CATEGORY_RULES`.
- **B2: Subject/body split.** `categorize_with_confidence()` was calling `keyword_hits` on the combined `searchable` string, so subject hits were re-counted as body hits and the dead-code `15 * max(0, ...)` term was always 0. Now accepts `subject`, `body_text`, and `sender_text` as truly separate fields and scores correctly (subject: 30, body: 20, sender: 15).

### ✨ Labeling Improvements

- **Q1: Shopping suppressed under Ads.** When Ads Promotions confidence ≥ 65, Shopping is dropped as redundant. Records `shopping_suppressed_under_ads` in `negative_reasons`.
- **Q3: Thread-aware labeling.** New `--use-thread-aware` flag. `load_thread_dominant_categories()` builds a thread_id → dominant_category map from existing SQLite decisions. In `decide()`, when a message lands in a catch-all (Review), it inherits the thread's dominant category at confidence 55. Never overrides a real keyword match or protected category.
- **Q7: Enriched AI review packets.** Packets now include `available_categories` (the full vocabulary), `sender_past_categories` (from profiles), and `thread_dominant_category` — context that helps the AI make better suggestions.

### 📚 Documentation

- README restructured into a clean 12-section layout with a full CLI reference and the AI-assisted review section.
- HANDOVER.md extended with two new sections: "How the script works" (end-to-end flow + why keyword rules, not embeddings) and "Architectural improvement suggestions" (embedding pre-classifier, trained classifier, thread modeling, sender reputation, calibration, module split).

### 🧪 Tests

- 45 tests passing. Added overlap-check, Shopping suppression, thread-aware inheritance (and non-override), and enriched-packet regression tests.

## 0.5.1 - 2026-07-06

### 🎯 Per-Category Confidence and Label Caps

- `categorize_with_confidence()` returns a 0–100 confidence per category. Categories below `--label-confidence` (default 50) are dropped unless protected/priority. `--max-labels-per-message` (default 3) caps applied labels; protected buckets are always kept. Adds `category_confidence` to `Decision`.

### 🧹 Body Cleaning

- `clean_body_text()` strips quoted reply chains, forwarded blocks, and footer/signature lines before category matching. A reply that quotes a promotional email is no longer misclassified as promo, and a long unsubscribe footer does not dominate the body. Unsubscribe link extraction still uses the raw body so footer URLs survive.

### 🔄 Relabel Workflow Improvements

- `--relabel-since-date` and `--relabel-label` restrict a relabel stage to a slice (by date or current Sorter label) without a full rescan.
- `--undo-relabel <run_id>` reverses a relabel run by swapping recorded adds/removes back. Each relabel apply records previous labels + run_id in the action ledger. Dry-run without `--apply`.
- `--relabel-run-id` resumes an interrupted relabel apply by skipping messages already recorded in the ledger for that run.

### ⚡ Body-Feature Cache Reuse

- `load_body_features_index()` precomputes cached body features; the worker fetches metadata-only for messages with cached features (when not `--refresh-existing`), and `decide()` reuses the cached body category hits so categorization stays body-aware without a re-fetch.

### 📚 Documentation Overhaul

- Rewrote the README into a standard, well-structured document: quick start, relabel workflow, labeling model, configuration, project layout, safety model, caps table, and performance controls.
- Cleaned up and consolidated the docs/ notes.

### 🧪 Tests

- 36 tests passing. Added regression coverage for confidence/cap behavior, body cleaning, undo relabel, resume-via-ledger, and cached-body-feature reuse.

## 0.5.0 - 2026-07-06

### 🏷️ Relabel Stage (read body, remove stale labels, re-apply)

- New `--stage relabel`. It reads each message's current `Sorter/*` labels, diffs them against the freshly computed desired categories, and issues one `batchModify` per group carrying both `addLabelIds` and `removeLabelIds`.
- Only labels in the `Sorter/` namespace are ever removed; user-created and Gmail system labels are never touched. A message that now only lands in a catch-all bucket has its stale `Sorter` labels cleared.
- Dry-run by default; `--apply` required to change Gmail. Each relabel is recorded in the action ledger.
- `--prune-empty-labels` deletes `Sorter/*` labels that no longer have any messages after a relabel apply.
- `manifests/relabel_manifest.json` writes a before→after preview using the live label list (works in dry-run).
- New dashboard "Relabel Review" section.

### 📖 Body-Aware Scanning

- New `--scan {metadata,full}`. In `full` mode the worker fetches `format=full` and `decide()` decodes a bounded slice of the body text and feeds it to `categorize()`, so labels can be assigned from body/header/footer content, not only subject+snippet. Ad confidence is still scored on headers+subject+snippet so a long promotional body does not inflate the trash score.
- Records `body_len` and `body_category_hits` per Decision and caches compact derived features (body length, category names hit in the body, unsubscribe count) in a new `message_features` SQLite table. Raw body text is never persisted, so a re-run can reuse body-derived features without re-fetching Gmail.

### 🧠 Sender→Category Profiles

- Added a `sender_profile` SQLite table accumulated from high-confidence and protected decisions. A precomputed profile index is consulted in `decide()` to add a category the subject keywords missed, so a re-run on an already-labeled mailbox self-improves: the first pass teaches the profile, the second pass uses it to fix keyword misses.
- New flags `--use-sender-profiles`/`--no-sender-profiles`, `--sender-profile-min-weight`, `--sender-profile-floor`.

### 🐛 Word-Boundary Keyword Matching

- `keyword_hits()` applies `\b` boundaries to word-like keywords and escaped substring matching to punctuation keywords. Fixes the substring bug that mislabeled mail: `exam` no longer matches `example.com`, `class` no longer matches `classification`, `sale` no longer matches `salon`. `categorize()`, `score_ad()`, and `is_perfect_ad_match()` switched to the new matcher.

### 🏗️ Architecture

- Split policy data and pure keyword matching into a `sorter/` package: `sorter/policy.py` (keyword lists, rules, precedence, defaults), `sorter/keywords.py` (word-boundary matcher), `sorter/config_loader.py` (optional `config/policy.yaml` overrides). `gmail_sorter.py` re-exports these names so `trash_rescue_audit.py`, `apply_domain_trash_policy.py`, and the tests keep working unchanged.
- Optional `config/policy.yaml` lets you override keyword groups and thresholds without editing code (requires PyYAML; falls back to built-in defaults if absent).
- Added `logging` throughout with a per-run log file under `data/runs/`.
- Added `SCHEMA_VERSION = 1` and a `schema_version` field on `Decision`/progress rows to support future migrations.

### 🧪 Tests

- 29 tests passing. Added regression coverage for word-boundary matching, sender-profile-assisted categorization, body-aware categorization, the relabel label diff (stale removal, user-label safety, empty-desired clear, no-op when correct), and empty-label pruning.

## 0.4.0 - 2026-07-06

### 📦 Safer Archive Stage

- Archive now requires an independent bulk-mail signal (List-Unsubscribe, List-Id, one-click unsubscribe, bulk/list precedence, campaign header, Gmail Promotions, or a body unsubscribe link) in addition to meeting `--archive-threshold`. A one-off message that only scored high on subject/snippet keywords is no longer pulled out of the inbox.
- Added `--archive-threshold` (default 65) so the archive confidence gate is tunable independently of the ad classification threshold.
- Added a recency guard `--archive-min-age-days` and `--archive-skip-unread` so recent or unread mail can be kept in the inbox during archive runs.
- Recorded a per-message `archive_reason` describing the evidence used, surfaced in a new dashboard "Archive Review" table.
- Added archive apply caps mirroring the trash controls: `--max-archive-total`, `--max-archive-per-domain`, and `--archive-canary-limit`.

### 🏷️ Cleaner Labeling

- Catch-all `Review` and `Updates` buckets are still shown on the dashboard but are no longer applied as `Sorter/Review` / `Sorter/Updates` Gmail labels, so generic mail stops getting tagged across the whole mailbox.
- Added a single `primary_category` per message (chosen by a protected/priority-first precedence) for cleaner filing and reporting, surfaced in the dashboard recent-sample table.

### 🧪 Tests

- Added regression tests for bulk-signal-gated archive, the unread archive guard, catch-all label skipping, primary-category precedence, and archive caps.

## 0.3.3 - 2026-07-05

### 📊 Benchmarks And Project Linkage

- Cross-linked Gmail Sorter with `Rad-ops/local-ai-coding-stack` so the mailbox workflow and local model stack are documented together.
- Added the live Qwen3.6 Trash rescue workload numbers to the README and integration notes: 6,531 reviewed rows, 10,309,912 prompt tokens, 846,873 generated tokens, 549.96 average prompt tok/sec, 90.92 average generation tok/sec, and 85.03% weighted draft-token acceptance.
- Pointed the full benchmark record to the AI stack repo, where benchmark CSVs belong.

### 🧑‍🔧 Human Documentation Pass

- Reworded generated-folder and ignore guidance so it explains why private mailbox artifacts stay local instead of simply listing excluded paths.
- Added `docs/LOCAL-AI-STACK-INTEGRATION.md` to describe how this repo uses the Qwen3.6 local review path without giving Gmail credentials to the model.
- Added `docs/NEXT-RUN-HANDOFF.md` so the next cleanup/commenting pass can continue without rediscovering the project state.

### 💬 Code Readability

- Added comments and docstrings around the main source files so a new developer can follow the policy pipeline, Gmail API boundaries, local-model review path, and permanent-delete gates without reading every branch from scratch.

## 0.3.2 - 2026-07-05

### ✨ GitHub Makeover

- Added a generated README hero image at `assets/gmail-sorter-hero.png`.
- Added emoji section markers, a clearer stage table, and direct links to decision/cleanup docs.
- Added `docs/DECISION-LOG.md` to explain the safety choices behind local AI review and staged deletion.
- Added `docs/CLEANUP-LOG-2026-07-05.md` to record local cleanup after the mailbox cleanup finished.

### 🧠 AI Stack Alignment

- Updated local LLM defaults and docs from the removed `coder-big` profile to `qwen36`.
- Updated `trash_rescue_audit.py` so `--start-local-llm` defaults to `qwen36`.
- Documented that Qwen3.6 is used here for bounded mailbox review while Gemma 4 belongs to the planner/architect slot in the broader local AI stack.
- Updated model-stack notes to match the Qwen3.6 primary, DeepSeek 32B reasoning fallback, and Gemma 4 planner stack.

### 🧹 Deep Clean

- Removed stale resume/tomorrow command notes from the repository.
- Cleaned generated local outputs from `reports/`, `manifests/`, and `data/`, keeping only `.gitkeep` placeholders.
- Cleaned Python bytecode/test caches from the local workspace.
- Expanded `.gitignore` for logs, JSONL, SQLite databases, and generated output.

### 🔒 Safety Rationale

- Generated reports/manifests/data are not committed because they can contain private mailbox metadata.
- Gmail OAuth credentials and tokens remain local only under `secrets/` and were not removed during cleanup.
- Permanent delete remains gated behind explicit flags and a rescue-audit path.

## 0.3.1 - 2026-07-05

- Added `--local-llm-all` to let Qwen review every audited Trash row, not only script-selected rescue-review candidates.
- Added llama.cpp timing capture and progress output for local LLM review speed, including prompt tok/sec, generation tok/sec, and draft-token acceptance when available.
- Added Qwen3.6 as the default local Trash rescue review profile in overnight commands.
- Added `apply_domain_trash_policy.py` for user-approved obvious-trash domains, with attachment and durable-record safeguards before permanent deletion.
- Added a separate full-mail-scope delete token path for Gmail `messages.delete`, plus manifest verification that checks whether Gmail still returns deleted message IDs.

## 0.3.0 - 2026-07-05

- Added separate `trash_rescue_audit.py` dry-run tool to double-check messages planned for Trash before permanently emptying Gmail Trash.
- Added rescue review reports, confidence-bucket labels, optional restore/apply flow, and optional OpenAI/web-assisted review for borderline candidates.
- Added local-LLM JSONL export/import workflow for Qwen-style offline double checking without giving the local model Gmail access.
- Added automated local llama.cpp review via `--local-llm`, with optional local profile startup and automatic result merge.
- Added resumable audit/model checkpoints and a no-sleep unattended command for deleting only messages where both script and local Qwen agree with 100% trash confidence.
- Reduced noisy Gmail 404 output during Trash rescue audits by summarizing stale/missing message IDs and writing them to a missing-ID file.
- Added SQLite state storage for message decisions plus an append-only action ledger for successful label/archive/trash changes.
- Added registered-domain sender intelligence so reports group noisy subdomains under one organization-level domain.
- Added domain review workflow outputs in CSV/JSON with suggested actions, storage impact, planned actions, protected counts, and sample subjects.
- Added storage reports that rank senders by estimated Gmail storage use and attachment impact.
- Added maintenance scan shortcuts with `--maintenance-days` and `--since-date`.
- Added priority categories and protected labels for immigration, studies, and real attachment mail, including known immigration contacts and IRCC/visa/work-permit terms.
- Split real attachments from inline image attachments so important files stay protected without overprotecting promotional image-only mail.
- Added trash safety controls with `--max-trash-per-domain`, `--max-trash-total`, and `--canary-limit`.
- Added local unit tests for registered-domain grouping, old progress compatibility, priority immigration detection, and attachment handling.

## 0.2.0 - 2026-07-05

- Hardened promotional/ad scoring with stronger subject-pattern detection, one-click unsubscribe headers, `List-Id`, bulk/list precedence, campaign headers, promotional sender local-parts, and auto-submitted system-mail penalties.
- Added `perfect_ad_match` detection for the safest trash candidates: 100 confidence, multiple independent bulk/promotional signals, promotional content evidence, and no protected-message disqualifiers.
- Kept trash protection conservative for allowlisted senders/domains, important or primary mail, attachments, protected categories, reply/forward threads, auto-submitted system mail, and mixed Gmail threads.
- Added body unsubscribe extraction from `text/html` and `text/plain` payloads when attachment details are enabled, while persisting only normalized unsubscribe/preference links instead of message body text.
- Split unsubscribe reporting into header and body sources, made unsubscribe targets clickable in dashboards, and added body unsubscribe link review tables.
- Added dashboard hardening for trash review: perfect-ad indicators, trash summary by sender domain, positive and negative reason summaries, applied counts, cached-outside-query counts, and safer pre-apply visibility.
- Added combined all-years dashboard output plus per-year dashboard files from the same scan.
- Filtered resumed progress to the current Gmail query so reused progress files do not pollute reports or manifests with stale cached decisions.
- Persisted apply-stage progress updates after label/archive/trash writes so interrupted apply runs can resume with more accurate local state.
- Added apply-stage progress logging for trash and label/archive writes.
- Added explicit Gmail HTTP timeouts through `httplib2.Http(timeout=...)`.
- Added clearer retry diagnostics with attempt counts and Gmail error text.
- Added `--apply-progress-every`, `--http-timeout`, and `--version`.
- Corrected trash apply status output so it reports messages with planned trash actions instead of the full decision set.
- Documented the safer all-years trash resume command without `--refresh-existing`.

## 0.1.0 - 2026-07-04

- Added Gmail OAuth setup, mailbox scanning, and staged decisions for label/archive/trash workflows.
- Added the first reporting outputs: HTML dashboard, CSV/JSON decision reports, sender summaries, manifests, and resumable progress files.
- Added allowlist/blocklist configuration so the cleanup policy could be tuned without editing Python.
- Added unsubscribe extraction, attachment review, and high-confidence promotional trash scoring.
- Added explicit `--apply` gating so scans were read-only unless the user asked for Gmail changes.
