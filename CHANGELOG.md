# Changelog

## 0.3.0 - 2026-07-05

- Added separate `trash_rescue_audit.py` dry-run tool to double-check messages planned for Trash before permanently emptying Gmail Trash.
- Added rescue review reports, confidence-bucket labels, optional restore/apply flow, and optional OpenAI/web-assisted review for borderline candidates.
- Added local-LLM JSONL export/import workflow for Qwen-style offline double checking without giving the local model Gmail access.
- Added automated local llama.cpp review via `--local-llm`, with optional `llm-switch coder-big` startup and automatic result merge.
- Added resumable audit/model checkpoints and a no-sleep unattended command for deleting only messages where both script and local Qwen agree with 100% trash confidence.
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

- Added staged Gmail classification, reporting, label/archive/trash application, resumable progress files, manifests, dashboard reporting, unsubscribe extraction, attachment review, and high-confidence promotional trash scoring.
