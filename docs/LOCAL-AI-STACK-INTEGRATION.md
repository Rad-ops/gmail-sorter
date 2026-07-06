# Local AI Stack Integration

Updated: 2026-07-05

Gmail Sorter is the real mailbox workflow. Local AI Coding Stack is the model/runtime notebook that supports it:

```text
https://github.com/Rad-ops/gmail-sorter
https://github.com/Rad-ops/local-ai-coding-stack
```

## What The Local Model Does Here

The sorter first applies normal Python rules: sender/domain signals, Gmail labels, unsubscribe headers, promotional language, attachment checks, and protected categories. The local model is a second reviewer for bounded Trash rescue packets.

The local model does not receive Gmail credentials, OAuth tokens, or direct mailbox access. It sees a small JSON-style packet with the fields needed for review, then returns a decision such as `keep_trash` or `rescue_review`.

## Current Model Roles

| Role | Model | Why it is used |
| --- | --- | --- |
| Mailbox review | Qwen3.6-35B-A3B-MTP | Fast enough for thousands of bounded review calls and strong enough for implementation-style classification. |
| Reasoning fallback | DeepSeek-R1-Distill-Qwen-32B | Kept in the broader stack for deeper reasoning checks. |
| Planner/architect | Gemma 4 26B MoE / 12B fallback | Kept outside the mailbox pipeline for architecture and planning work. |

## Benchmark To Keep With The AI Stack

The live 2026-07-05 Qwen3.6 Trash rescue run produced:

- 6,531 reviewed rows
- 10,309,912 prompt tokens
- 846,873 generated tokens
- 549.96 average prompt tok/sec
- 90.92 average generation tok/sec
- 85.03% weighted draft-token acceptance

The CSV lives in the AI stack repo:

```text
benchmarks/gmail-sorter-local-llm-summary-2026-07-05.csv
```

Per-message prompt/result files are intentionally not committed because they may contain private mailbox metadata.
