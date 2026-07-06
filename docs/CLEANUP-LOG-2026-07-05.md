# 🧹 Cleanup Log - 2026-07-05

## Removed From Local Workspace

| Path | Why |
| --- | --- |
| `reports/*` | Generated private HTML/CSV/JSON mailbox reports. |
| `manifests/*` | Generated private action manifests. |
| `data/*` | Generated resumable progress and state cache. |
| `__pycache__/`, `src/__pycache__/`, `tests/__pycache__/` | Python bytecode cache. |
| `.pytest_cache/` | Test cache. |
| `command to resume.txt`, `resume for 2026-07-05`, `tbresumed` | One-off stale operational notes. |

## Kept

| Path | Why |
| --- | --- |
| `secrets/*.json` | Required local OAuth credentials/tokens, ignored by Git. |
| `reports/.gitkeep`, `manifests/.gitkeep`, `data/.gitkeep` | Empty directory placeholders for GitHub. |
| `assets/gmail-sorter-hero.png` | Generated README banner. |

## Rule Going Forward

Commit code, docs, configs, tests, and reusable assets. Do not commit mailbox reports, manifests, progress databases, tokens, or one-off run notes.
