# Local AI Stack Integration

Updated: 2026-07-06

Gmail Sorter is the mailbox workflow; the
[local AI coding stack](https://github.com/Rad-ops/local-ai-coding-stack) is the
model/runtime notebook that supports it.

## How the local model is used

1. The sorter applies Python rules first: sender/domain signals, Gmail labels,
   unsubscribe headers, promotional language, attachment checks, and protected
   categories.
2. For Trash rescue, the sorter exports **bounded review packets** (subject,
   sender, snippet, a short body excerpt, and the sorter's reasons) — never
   Gmail credentials, OAuth tokens, or direct mailbox access.
3. The local model returns a decision such as `keep_trash` or `rescue_review`
   with a confidence. Both the heuristic and the model must agree before a
   permanent-delete manifest is produced.

## Model roles

| Role | Model | Why |
| --- | --- | --- |
| Mailbox review | Qwen3.6-35B-A3B-MTP | Fast enough for thousands of bounded review calls; strong on implementation-style classification. |
| Reasoning fallback | DeepSeek-R1-Distill-Qwen-32B | Deeper reasoning checks in the broader stack. |
| Planner/architect | Gemma 4 26B MoE | Kept outside the mailbox pipeline for architecture and planning. |

## Recorded benchmark (2026-07-05)

The live Qwen3.6 Trash rescue run:

- 6,531 reviewed rows
- 10,309,912 prompt tokens · 846,873 generated tokens
- 549.96 avg prompt tok/sec · 90.92 avg generation tok/sec
- 85.03% weighted draft-token acceptance

The benchmark CSV lives in the AI stack repo at
`benchmarks/gmail-sorter-local-llm-summary-2026-07-05.csv`. Per-message
prompt/result files are not committed because they may contain private mailbox
metadata.

