---
name: Nightly Backup Verify
schedule: "30 2 * * *"
description: Verify last night's backup archive exists and its checksum matches the manifest.
---

1. Run `bash scripts/verify_backup.sh /backups/latest.tar.gz`.
2. Compare the printed SHA-256 with the value stored in `state/backup_manifest.json`.
3. If they differ or the archive is missing, send a Telegram alert: "Backup verify FAILED: <reason>".
4. Write today's result to `state/backup_last.json`.
