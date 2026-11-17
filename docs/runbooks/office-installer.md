# Office laptop: native installer (no Docker)

`NilPsikoloji-KlinikKasa-Kurulum-<version>.exe` installs Klinik Kasa on the office laptop as
real Windows services. Everything starts at boot, **before anyone logs in**. This replaces the
Docker/WSL setup in office-laptop-windows.md (still valid for development).

## Decisions behind it (2026-11-17)

- **Native Windows services** instead of Docker.
- **No Tailscale.** Klinik Kasa is used and edited **only on the office laptop**
  (http://127.0.0.1:8090). From home, each therapist reads a **daily encrypted snapshot**
  (17:00) in the clinic's Google Drive. Booking stays available everywhere in Klinik Web.

## What gets installed

| Service | What | Listens on |
|---|---|---|
| KlinikPostgres | PostgreSQL 16 (bundled) | 127.0.0.1:55432 only |
| KlinikKasa | Klinik Kasa web app | 127.0.0.1:8090 only |
| KlinikSync | sync worker (outbound HTTPS to Klinik Web) | 127.0.0.1:8091 (health) |
| KlinikJobs | backups 02:30 + hourly 09–21, snapshot 17:00, weekly cloud copy | — |

| Folder | Holds |
|---|---|
| `C:\Program Files\Nil Psikoloji\Klinik Kasa` | programs (replaced on update) |
| `C:\ProgramData\Nil Psikoloji\Klinik\pgdata` | the database |
| `...\Klinik\config\klinik.env` | keys and passwords (SYSTEM + Administrators only) |
| `...\Klinik\backups`, `...\Klinik\ozet` | encrypted backups, encrypted snapshots |
| `...\Klinik\logs` | setup and service logs |

## Before installing

1. Windows 11 settings from office-laptop-windows.md §1: BitLocker, never sleep, lid does nothing,
   Windows Update active hours.
2. From the cloud setup (cloud.md): the Klinik Web address, a **sync token**
   (`create-sync-token`), and the **users.toml** used for the cloud.
3. The clinic's Google account (for backups and snapshots).

## Installing

Run the .exe as an administrator and answer three pages:

1. **Klinik Web bağlantısı**: address and sync token (can be added later by running the
   installer again).
2. **Kullanıcılar**: the users.toml file.
3. **Google Drive**: leave ticked. A browser window opens; sign in with the clinic's Google
   account and allow access. rclone only sees the files it creates (`drive.file`).

A console window shows the steps. At the end:

- **NIL-KLINIK-ANAHTARLAR.txt** opens on the desktop. It holds the note key, the backup private
  key, the 2FA key, the database password and the users' temporary passwords.
  **Print it twice, store the copies in two safe places, then delete the file and empty the
  Recycle Bin.** The backup private key exists nowhere else.
- Klinik Kasa opens at http://127.0.0.1:8090 (also a desktop shortcut).

## First day

1. Each therapist logs in with the temporary password, chooses a new one and sets up 2FA.
2. Each therapist opens **Günlük özet** and chooses a snapshot passphrase (different from the
   login password). From 17:00 that day, `Nil Psikoloji/ozet/nil-ozet-<user>-<date>.html`
   appears in Google Drive.
3. Test from home: open Google Drive, **download** the file, open it in a browser (on Android:
   Chrome), type the passphrase.
4. Ayarlar → Yedekleme: the first backup appears within minutes of installing.

## Updating

Run the newer installer. It finds the existing database, keeps all data and keys, applies
migrations and restarts the services.

## Monthly restore test (no Docker needed)

With the printed backup private key typed into a file on a USB stick (`identity.txt`, one line
starting with `AGE-SECRET-KEY-`), in an administrator PowerShell:

```powershell
$k = 'C:\Program Files\Nil Psikoloji\Klinik Kasa'
& "$k\python\python.exe" -m klinik_local.service --env 'C:\ProgramData\Nil Psikoloji\Klinik\config\klinik.env' job restore-test --identity E:\identity.txt
```

Expect `RESULT: OK`. The result also appears in Ayarlar → Yedekleme. Remove the USB stick.

## Uninstalling

Settings → Apps → Nil Psikoloji Klinik Kasa. The services are removed; **data, backups and keys
in ProgramData are kept** (reinstalling picks them up). Delete that folder by hand only when the
clinic no longer needs the data, after making sure a backup exists elsewhere.

## Rebuilding after losing the laptop

1. Install on the new laptop (new sync token from the cloud, same users.toml).
2. Stop KlinikKasa, KlinikSync and KlinikJobs. Restore the newest backup into the database with
   the private key (`job restore-test` checks a backup; a full restore guide is in backups.md).
3. Start the services. The sync worker compares client codes with the cloud and never reissues
   one the cloud already knows.
