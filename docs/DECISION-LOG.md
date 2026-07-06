# Decision Log

Updated: 2026-07-06

## Design principle

Gmail Sorter is built around conservative cleanup. The tool can identify
low-value mail quickly, but every destructive path has visible reports,
manifests, and explicit flags before Gmail is changed.

## Key decisions

| Question | Decision | Reasoning |
| --- | --- | --- |
| Should the sorter permanently delete directly from scan results? | No. | It stages labels/archive/trash first, then uses rescue audits before permanent delete. |
| Should local AI see Gmail directly? | No. | The local model receives bounded JSONL review packets or local server calls, not Gmail credentials. |
| Should generated reports be committed? | No. | They can contain private mail metadata and are gitignored. Only `.gitkeep` placeholders stay. |
| Should archive trigger on a high ad score alone? | No. | Archive requires an independent bulk-mail signal so one-off high-scoring mail is not pulled from the inbox. |
| Should relabel touch user-created labels? | No. | Relabel only adds/removes `Sorter/*` labels. User and Gmail system labels are never touched. |
| Should keyword matching use substrings? | No. | Word-boundary matching prevents `exam` matching `example.com` and `class` matching `classification`. |
| Should every matched category be applied? | No. | Per-category confidence and a per-message cap prevent label sprawl; protected buckets are always kept. |

## AI review stack

```
Sorter rules → Trash rescue audit → Local Qwen3.6 review
  → Both reviewers agree? → 100% safe: permanent-delete manifest
                           → Anything risky: rescue review / restore labels
```

The sorter uses Qwen3.6 for local mailbox review because this is an
implementation/review task with bounded inputs. The broader local AI stack keeps
Gemma 4 as a planner/architect model and DeepSeek 32B as a reasoning fallback.

## Protection model

Protected messages are never archived or trashed when they are:

- Allowlisted (sender email or domain in `config/allowlist.txt`)
- Important, starred, or primary
- Have real attachments (PDFs/documents — inline marketing images are not
  "real" attachments)
- Match a protected category: immigration, studies, finance, account security,
  health, government/legal, utilities, insurance, receipts/orders, work/school

Immigration signals include IRCC/visa/work permit/permanent residence terms and
known lawyer/contact names (Pinaz Marolia, Tiffani, Ronen, Raquel, Jemma,
Jonalyn, Oskoii).
