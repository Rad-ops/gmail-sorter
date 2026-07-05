# Commands

Run the overnight local-Qwen safety review:

```bash
commands/run-overnight-trash-rescue.sh
```

After reviewing `reports/trash_rescue_audit_local_qwen.html`, restore rescue candidates:

```bash
commands/restore-reviewed-candidates.sh
```

The restore command requires explicit restore flags inside the script and does not permanently delete mail.
