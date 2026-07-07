# How to Run Gmail Sorter v0.7.0 (and v0.8.0) on Your Mailbox

This document is a step-by-step playbook for running the Gmail Sorter
against your real Gmail mailbox. It targets CachyOS Linux with the
**fish** shell, but every command also works on any Arch-based
distribution (Manjaro, Endeavour, Garuda) or any other Linux with
Python 3.11+ installed. macOS users can adapt the path names.

> **Pick the right release for you:**
> - **v0.7.0** is the latest stable release. The "smarter classifier"
>   milestone: real body in centroids, multi-language (EN+FR+FA), AI
>   active learning + AI removal, sender profile time-decay.
> - **v0.8.0** is the next planned release (per-keyword learned weights,
>   thread conversation modeling, sender reputation, Gmail History API,
>   better HTML body extraction). It is **not yet shipped** — this
>   document shows the commands you'll run once v0.8.0 lands on
>   `main`. v0.7.0 is the right choice today.

The commands assume you are inside the project root:
`/home/rzangeneh/codebase/sorter`. If you cloned the repo elsewhere,
substitute your path.

---

## 1. Prerequisites

| What | Why | How (CachyOS) |
|---|---|---|
| Python 3.11+ | runs the sorter | `sudo pacman -S python python-pip` (already present on most CachyOS installs) |
| `python-virtualenv` | creates `.venv` | `sudo pacman -S python-virtualenv` |
| PyYAML | parses `policy.yaml`, `policy.fr.yaml`, `policy.fa.yaml` | `sudo pacman -S python-yaml` (or `pip install pyyaml`) |
| `tldextract` | registered-domain grouping | `pip install tldextract` |
| `google-api-python-client` | Gmail API client | `pip install google-api-python-client` |
| `google-auth-*` | OAuth flow | `pip install google-auth google-auth-oauthlib google-auth-httplib2` |
| `gh` (GitHub CLI) | opens PRs, watches the repo | `sudo pacman -S github-cli` and `gh auth login` |
| local llama.cpp server (optional) | the embedding pre-classifier + AI review path | see the local-ai-coding-stack repo |

If you already had a working `.venv` from a prior v0.5.x or v0.6.x run,
your dependencies are already installed; the v0.7 schema migration
takes care of the rest.

---

## 2. First-time setup on CachyOS / fish

```fish
# 2.1 Open a fish terminal and go to the project.
cd /home/rzangeneh/codebase/sorter

# 2.2 (Re)create the virtualenv. fish uses the same python as bash.
#     Drop --system-site-packages if you prefer a fully isolated env.
python -m venv .venv --system-site-packages

# 2.3 Activate the venv. fish and bash disagree on the activate path;
#     fish uses the .fish variant. Both shells work, but the fish
#     variant gives you nicer prompt integration.
source .venv/bin/activate.fish

# 2.4 Install Python dependencies into the venv.
pip install -r requirements.txt

# 2.5 (Optional but recommended) install langdetect so the FR/FA
#     detector is more accurate than the pure-Python fallback.
pip install langdetect

# 2.6 Drop the Gmail OAuth client secret at the documented path.
#     The file is gitignored. Download it from
#     https://console.cloud.google.com/apis/credentials (Desktop app
#     OAuth client, JSON) and copy it here:
cp ~/Downloads/client_secret_*.json secrets/credentials.json
```

If you are on bash or zsh, the only difference is the activate line:

```bash
source .venv/bin/activate
```

---

## 3. First scan (read-only classification, no Gmail changes)

The default run is dry-run only — it never touches Gmail. Use this to
get the dashboard, see what the sorter thinks, and confirm the OAuth
flow works.

```fish
# 3.1 From the project root, with the venv active:
.venv/bin/python src/gmail_sorter.py --resume
```

What this does:
- Lists every Gmail message matching the default query
  (`before:2025/12/30 -in:trash`).
- Classifies each message (decision only — no Gmail writes).
- Writes `reports/gmail_sorter_report.html`, CSV/JSON reports,
  manifests, and SQLite state in `data/`.
- Runs the v0.7 schema migration if your SQLite DB is older.

Open `reports/gmail_sorter_report.html` in a browser. Verify the OAuth
consent screen appeared on first run (token is saved to
`secrets/token_readonly.json` after you approve).

> **CachyOS tip:** `xdg-open reports/gmail_sorter_report.html` opens
> the dashboard in your default browser.

---

## 4. Apply labels (writes to Gmail)

After the dry-run looks right, apply the labels. This is the lowest-risk
apply stage: it adds `Sorter/<category>` labels but does not archive or
trash anything.

```fish
# 4.1 Dry-run + apply labels in one command.
.venv/bin/python src/gmail_sorter.py --stage label --apply --resume
```

What this does:
- Reads the cached decisions from step 3.
- Computes the Sorter/* labels that would be applied.
- One Gmail `batchModify` call per group of messages.
- Records every write in the `action_ledger` table.

> **Rollback:** label apply does not have a built-in undo. The
> `action_ledger` table records what was added, so a follow-up script
> can remove the labels if you change your mind. The next run with
> `--stage relabel` will *correct* any over-application.

---

## 5. Body-aware rescan (for relabeling)

The first scan only used headers + snippet. v0.7 makes the second scan
*body-aware* by reading each message's body text. The cleaned body
excerpt is persisted to `data/.../state.sqlite` so subsequent re-runs
are fast (no re-fetching from Gmail).

```fish
# 5.1 Full body-aware rescan. The --refresh-existing flag forces a
#     re-fetch of every message even if you have a cached decision.
.venv/bin/python src/gmail_sorter.py --scan full --resume --refresh-existing
```

Open the dashboard again. The categories should be richer because the
classifier now sees the body text. The v0.7 embedding centroids will
re-learn from real body semantics on this run.

---

## 6. Relabel (replace stale Sorter/* labels)

A relabel pass reads each message's current `Sorter/*` labels, diffs
them against the freshly computed desired labels, and removes stale
labels while keeping correct ones.

```fish
# 6.1 Dry-run (no Gmail writes): the relabel manifest is written
#     to manifests/relabel_manifest.json.
.venv/bin/python src/gmail_sorter.py \
    --stage relabel \
    --scan full \
    --resume \
    --refresh-existing

# 6.2 Apply. --prune-empty-labels deletes any Sorter/* label that no
#     longer has any messages after the relabel.
.venv/bin/python src/gmail_sorter.py \
    --stage relabel \
    --scan full \
    --apply \
    --resume \
    --prune-empty-labels

# 6.3 Undo a bad relabel run. The apply step prints a run_id; use
#     that here.
.venv/bin/python src/gmail_sorter.py --undo-relabel <run_id> --apply

# 6.4 Resume an interrupted relabel.
.venv/bin/python src/gmail_sorter.py \
    --stage relabel \
    --scan full \
    --apply \
    --resume \
    --relabel-run-id <run_id>

# 6.5 Relabel only a slice — by date or by current Sorter label.
.venv/bin/python src/gmail_sorter.py \
    --stage relabel \
    --scan full \
    --apply \
    --resume \
    --relabel-label Review
.venv/bin/python src/gmail_sorter.py \
    --stage relabel \
    --scan full \
    --apply \
    --resume \
    --relabel-since-date 2024-01-01
```

---

## 7. AI review workflow (v0.7 active learning)

This is the loop the user mentioned in the original brief: the code
classifies, the AI reviews the low-confidence decisions, and the AI's
verified decisions are pushed back into the local state so the next
scan benefits automatically.

```fish
# 7.1 Export low-confidence decisions as JSONL for the AI to review.
#     Bounded to ~1200-char body excerpts; never ships raw body text.
.venv/bin/python src/gmail_sorter.py \
    --scan full \
    --resume \
    --refresh-existing \
    --export-ai-review

# 7.2 Hand the JSONL to the AI. The file lives at:
#     data/label_review_packets.jsonl
#     Each line is a JSON object with sender, subject, body_excerpt,
#     the code's categories/confidence/reasons, and empty ai_* fields.
#     The AI fills ai_label, ai_confidence, ai_reason, ai_reviewed=true.
#     The AI must never suggest removing a protected category.

# 7.3 Once the JSONL is reviewed, merge and apply.
#     v0.7 active learning runs after the merge: the AI's verified
#     decisions are pushed into sender_profile and, when an embedding
#     backend is on, the category centroids. The next scan benefits
#     automatically.
.venv/bin/python src/gmail_sorter.py \
    --stage relabel \
    --scan full \
    --apply \
    --resume \
    --merge-ai-labels
```

The merge step prints three numbers: `agreed`, `added`, `removed`.
- `agreed` — the AI's label matches what the code already assigned.
- `added` — the AI added a label the code missed (confidence >= 0.7).
- `removed` — the AI removed a non-protected label the code assigned
  (confidence >= 0.85, the stricter removal threshold).

If you want to skip the active-learning pass for a one-off run:

```fish
.venv/bin/python src/gmail_sorter.py \
    --stage relabel \
    --scan full \
    --apply \
    --resume \
    --merge-ai-labels \
    --no-ai-learning
```

---

## 8. Embedding pre-classifier (v0.6, refined in v0.7)

The embedding pre-classifier needs a local embeddings endpoint. The
easiest path is the local llama.cpp server's `/v1/embeddings` route,
already running as a systemd user service on this machine.

```fish
# 8.1 Confirm the embeddings endpoint is up:
curl -s http://127.0.0.1:8080/v1/embeddings \
    -H "Content-Type: application/json" \
    -d '{"model": "local", "input": "test"}' | head -c 200
echo

# 8.2 If the curl returns a JSON array, run the sorter with
#     --use-embeddings. The centroids are loaded from
#     data/gmail_sorter_state.sqlite (table: category_centroid).
#     If the table is empty, the first run learns the centroids; the
#     second run benefits from them.
.venv/bin/python src/gmail_sorter.py \
    --scan full \
    --resume \
    --refresh-existing \
    --use-embeddings

# 8.3 Run a relabel that uses the embedding-augmented classifier.
.venv/bin/python src/gmail_sorter.py \
    --stage relabel \
    --scan full \
    --apply \
    --resume \
    --use-embeddings
```

> **No embeddings endpoint?** The sorter falls back to keyword-only
> classification automatically; the run still completes. The
> `--use-embeddings` flag is opt-in.

---

## 9. Multi-language (EN + FR + FA)

v0.7 picks a language overlay per message. The detector is automatic
— no flag required. To inspect what the detector picked, look at the
`detected_language` field in `data/.../state.sqlite` (column on
`messages`) or the JSON progress file.

If you want to add or edit language keywords, edit the YAML files:

- `config/policy.fr.yaml` — French IRCC, finance, health, etc.
- `config/policy.fa.yaml` — Farsi equivalents.

Both files are **additive by default**: French keywords extend the
matching English category list. To replace a category's keywords for a
specific language, set `replace: true` on the category entry.

```fish
# 9.1 (Optional) install langdetect for higher accuracy on short
#     subjects. Without langdetect the sorter uses a pure-Python
#     stopword-frequency fallback that is also accurate but slower to
#     update.
pip install langdetect
```

---

## 10. Archive and trash stages (high risk)

These stages actually move mail out of your inbox or to Trash. Read
the safety model in `HANDOVER.md` section 9 first.

```fish
# 10.1 Archive — requires an independent bulk-mail signal (List-
#     Unsubscribe, List-Id, one-click unsubscribe, bulk precedence,
#     campaign header, Gmail Promotions, or body unsubscribe link).
#     Plus --archive-threshold confidence.
.venv/bin/python src/gmail_sorter.py \
    --stage archive \
    --apply \
    --resume \
    --archive-skip-unread \
    --archive-min-age-days 30

# 10.2 Trash — requires --trash-obvious-ads AND --i-understand-trash.
#     Plus --trash-threshold confidence (default 90) or a perfect ad
#     match. Caps: --max-trash-per-domain, --max-trash-total,
#     --canary-limit.
.venv/bin/python src/gmail_sorter.py \
    --stage trash \
    --apply \
    --trash-obvious-ads \
    --i-understand-trash \
    --resume
```

**Before you trash**, run `src/trash_rescue_audit.py` against the
planned-trash set. It re-fetches every message, double-checks the
priority/attachment/durable-record signals, and can export bounded
packets for the local Qwen model. The audit is the safety net
between the sorter and `messages.delete`.

---

## 11. Maintenance mode (small, frequent runs)

After the big cleanup, you want periodic small runs. v0.7 has the
infrastructure (cache, sender profiles, embedding centroids) so a
weekly maintenance scan is fast.

```fish
# 11.1 Catch up on the last 7 days of mail, classify, no apply.
.venv/bin/python src/gmail_sorter.py --maintenance-days 7 --resume

# 11.2 Catch up since a specific date.
.venv/bin/python src/gmail_sorter.py --since-date 2026-07-01 --resume

# 11.3 Maintenance + apply labels (lowest risk apply stage).
.venv/bin/python src/gmail_sorter.py \
    --maintenance-days 7 \
    --resume \
    --stage label \
    --apply
```

> **v0.8.0 (when it lands) will add a `--since-history-id <id>` flag**
> that uses the Gmail History API to fetch only messages added/changed
> since the last scan. This is what makes the weekly cadence fast
> enough to run unattended.

---

## 12. Tests

```fish
# 12.1 Run the full test suite from the project root. v0.7.0 ships
#     with 125 tests; should finish in <1 second.
.venv/bin/python -m unittest discover -s tests

# 12.2 Run a single test file.
.venv/bin/python -m unittest tests.test_lang

# 12.3 Run a single test method.
.venv/bin/python -m unittest tests.test_ai_learning.AILearningTests.test_learning_writes_sender_profile

# 12.4 (Optional) py_compile every Python file to catch syntax
#     errors before a real run.
.venv/bin/python -m py_compile src/*.py src/sorter/*.py tests/*.py
```

A clean run prints `OK` and `Ran 125 tests in 0.NNNs`.

---

## 13. What v0.8.0 will change (preview)

v0.8.0 is the heuristics-and-performance release. When it ships, the
key new capabilities are:

| Flag | What it does |
|---|---|
| `--use-learned-weights` | Replaces the hand-tuned 30/20/15 keyword weights with weights learned from the labeled data in `messages`. |
| `--since-history-id <id>` | Incremental scan via the Gmail History API. 100x faster on a weekly cadence. |
| `--use-sender-reputation` (default on) | First-class sender reputation: total messages, ad fraction, derived score. Auto-suggests blocklist candidates. |
| `--use-html-body` (default on) | Better HTML body extraction: tables, quoted-printable decoding, multi-part MIME. |

Until v0.8.0 lands, the v0.7 commands above are the full set.

---

## 14. Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'google'` | `pip install -r requirements.txt` inside the venv |
| `ImportError: No module named 'yaml'` | `pip install pyyaml` (or `sudo pacman -S python-yaml`) |
| `sqlite3.OperationalError: no such column: body_text_excerpt` | v0.7 needs a fresh SQLite DB or the migration. If you cloned a pre-v0.7 DB, run the sorter once; the migration runs on `open_state_db()`. |
| `OAuth consent screen does not appear` | Open `secrets/credentials.json` and confirm it is a Desktop-app OAuth client. Re-run with `--open-browser`. |
| `Centroid embedding failed` (in the logs) | The local LLM server isn't running. The sorter falls back to keyword-only — run still completes. |
| `--use-embeddings` produces no centroids | A category needs at least 3 high-confidence decisions to learn a centroid. Run twice: first run learns, second run uses. |
| `AI review merge says "0 added, 0 removed"` | Either no decisions matched the threshold, or the AI didn't fill in `ai_reviewed=true`. Open `data/label_review_packets.jsonl` and check. |
| `Schema version mismatch` | v0.7 supports pre-v0.7 files via migration. If you see this, file an issue with the output of `python -c "import sqlite3; print(sqlite3.connect('data/gmail_sorter_state.sqlite').execute('SELECT MAX(version) FROM schema_migrations').fetchone())"`. |

---

## 15. Quick reference (the four commands you'll use most)

```fish
# 15.1 Read-only full rescan.
.venv/bin/python src/gmail_sorter.py --scan full --resume --refresh-existing

# 15.2 Relabel (dry-run, then apply).
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --resume --refresh-existing
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --apply --resume --prune-empty-labels

# 15.3 AI review loop.
.venv/bin/python src/gmail_sorter.py --scan full --resume --refresh-existing --export-ai-review
# ... hand the JSONL to the AI ...
.venv/bin/python src/gmail_sorter.py --stage relabel --scan full --apply --resume --merge-ai-labels

# 15.4 Weekly maintenance.
.venv/bin/python src/gmail_sorter.py --maintenance-days 7 --resume
```

That is the entire workflow. Save this file, keep it open in a fish
tab while you run the sorter, and you will not need to re-read the
handover for every command.
