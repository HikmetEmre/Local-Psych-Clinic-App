# Backups, keys and the monthly restore test

## What is backed up

| What | When | Kept | Where |
|---|---|---|---|
| Office database (all clients, sessions, notes) | 02:30 nightly | 30 nights + 12 monthly | `backups/` on the laptop and Google Drive |
| Office database | hourly 09:00–21:00 | 48 | same |
| Cloud database (schema `app`) | Sunday 03:30, if configured | 8 | same |
| Cloud database | continuous | Supabase plan | Supabase |

Every file is encrypted with **age** to a public key before it touches the disk. Notes inside
are additionally Fernet-encrypted. Ayarlar → Yedekleme shows the last successful run, the last
upload, the last error and the last restore test, and warns when any of them is overdue.

## The keys: who holds what

| Key | Opens | Lives on the laptop? | Offline copy |
|---|---|---|---|
| age **private** key (`age-keygen` output) | backup files | **No, never** | 2 copies |
| `LOCAL_FERNET_KEYS` | client contact details, notes | yes (`.env`) | 2 copies |
| `LOCAL_AUTH_FERNET_KEY` | stored 2FA secrets | yes | 1 copy |
| BitLocker recovery key | the laptop disk | no | 2 copies |
| Supabase / cloud secrets | the cloud | no | password manager |

Two offline copies: a printed sheet and a USB stick, in two different safe places (for
example the clinic safe and the admin's home). **Losing `LOCAL_FERNET_KEYS` means losing every
note, even with perfect backups.** Check the copies once a year.

Create the backup key pair once, on a computer that is not the laptop:
```sh
docker run --rm --entrypoint age-keygen klinik-backup > age-identity.txt
```
Put the `public key: age1...` line in `.env` as `BACKUP_AGE_RECIPIENT`, then move
`age-identity.txt` to the USB stick and delete it from that computer.

## Google Drive

`rclone config` once (remote type *drive*, scope *drive.file*), then copy `rclone.conf` to
`secrets/rclone/rclone.conf` next to the repository and set `BACKUP_RCLONE_REMOTE`, e.g.
`gdrive:klinik-yedek`. The folder mirrors the local retention.

## Monthly restore test (about 5 minutes)

1. Plug in the USB stick with `age-identity.txt`.
2. On the laptop, in the repository folder:
   ```sh
   bash infra/backup/restore_test.sh /path/to/age-identity.txt
   ```
3. Expect `RESTORE TEST PASSED`. The script decrypts the newest nightly backup, restores it
   into a throwaway database (deleted afterwards), checks the schema version and the row
   counts, and decrypts client names and notes with the current keys. The result appears in
   Ayarlar → Yedekleme.
4. Unplug the USB stick and put it back in the safe.

If it fails: do not ignore it. Check Ayarlar → Yedekleme for the last error, run a backup by
hand (`docker compose ... exec backup klinik-backup daily`) and test again.
