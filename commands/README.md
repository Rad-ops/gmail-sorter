# ⚙️ Commands

Run the overnight Qwen3.6 safety review:

```bash
commands/run-overnight-trash-rescue.sh
```

Run the overnight Qwen3.6 safety review and permanently delete only messages that both reviewers classify as 100% safe trash:

```bash
commands/run-overnight-trash-rescue-and-delete-verified.sh
```

After reviewing `reports/trash_rescue_audit_local_qwen.html`, restore rescue candidates:

```bash
commands/restore-reviewed-candidates.sh
```

The restore command requires explicit restore flags inside the script and does not permanently delete mail.
