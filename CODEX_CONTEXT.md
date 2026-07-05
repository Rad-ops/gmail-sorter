# Codex Context

Read this before working in this repo.

Repo path:

```text
/home/rzangeneh/codebase/sorter
```

GitHub remote:

```text
https://github.com/Rad-ops/gmail-sorter.git
```

Do not commit:

- `secrets/*.json`
- `data/*`
- `reports/*`
- `manifests/*`
- `.venv/`

Current critical workflow:

```bash
/home/rzangeneh/codebase/local-ai-gmail-interpreter/commands/run-overnight-trash-rescue.sh
```

Primary scripts:

- `src/gmail_sorter.py` - Gmail classifier/apply/report tool.
- `src/trash_rescue_audit.py` - Trash safety audit and local-Qwen review tool.

Local AI stack:

- Keep under `/home/rzangeneh/ai`.
- OpenAI-compatible endpoint: `http://127.0.0.1:8080/v1`.
- Preferred profile for long Gmail review: `llm-switch coder-big`.

Safety priorities:

- Immigration/legal messages must be protected.
- Studies/school messages must be protected.
- Real attachments must be protected.
- Known immigration terms include Pinaz Marolia, Tiffani, Ronen, Raquel, Jemma, Jonalyn, Oskoii/Oskooii/Oskoui, IRCC, visa, work permit, study permit, permanent residence, biometrics.

Verification:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile src/gmail_sorter.py src/trash_rescue_audit.py
```
