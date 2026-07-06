# Next Run Handoff

Updated: 2026-07-05

## Current Repos

- Gmail Sorter: `https://github.com/Rad-ops/gmail-sorter`
- Local AI Coding Stack: `https://github.com/Rad-ops/local-ai-coding-stack`

## What Was Just Updated

- Gmail Sorter version target: `0.3.3`
- Local AI stack version target: `0.2.1`
- The Qwen3.6 Gmail Sorter workload benchmark was copied into the AI stack repo and summarized here.
- Ignore/privacy docs were rewritten to explain why private generated files are excluded.
- The repos now point at each other so readers understand that Gmail Sorter is the workload and Local AI Coding Stack is the runtime/model notebook.

## Code Commenting Goal

The code should be readable by a new developer learning how the project works. Prefer comments that explain intent and safety boundaries:

- why a branch protects mail from archive/trash
- why a Gmail API call uses a specific token or scope
- why generated files stay local
- why local model packets are bounded
- where permanent delete is gated

Do not comment every assignment mechanically. The useful comments are the ones that prevent a future maintainer from missing a safety rule.

## Validate After Edits

```bash
cd /home/rzangeneh/codebase/sorter
.venv/bin/python -m py_compile src/*.py tests/*.py
.venv/bin/python -m unittest discover -s tests
```
