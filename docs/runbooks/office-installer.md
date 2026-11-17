# Installer: office computer and the owner's laptop

`NilPsikoloji-Klinik-Kurulum-<version>.exe` installs the clinic program as real Windows
services. Everything starts at boot, **before anyone logs in**.

## Decisions behind it (2026-11-17/18)

- **One program on the office computer**, two roles: **Yönetici** (the owner: sees
  everything) and **Terapist** (whole calendar, own clinical records, own income). No cloud.
- **The owner's laptop** gets a **copy that works offline**. On the office network it syncs
  with the office every minute (two ways). The office is authoritative: when the same record
  was changed on both, the office's version stays and the laptop's waits in
  Ayarlar → Çakışmalar for the owner to decide.
- **Native Windows services** instead of Docker. **No Tailscale.** From home, therapists read
  the **daily encrypted snapshot** (17:00) in the clinic's Google Drive.

## What gets installed

| Service | Office computer | Owner's laptop |
|---|---|---|
| KlinikPostgres | PostgreSQL 16, 127.0.0.1:55432 only | same |
| KlinikApp | https://localhost:8443, and the office network (laptop sync) | https://localhost:8443, this computer only |
| KlinikJobs | backups 02:30 + hourly 09–21, snapshot 17:00 | — (backups are made at the office) |

| Folder | Holds |
|---|---|
| `C:\Program Files\Nil Psikoloji\Klinik Kasa` | programs (replaced on update) |
| `C:\ProgramData\Nil Psikoloji\Klinik\pgdata` | the database |
| `...\Klinik\config\klinik.env` | keys and passwords (SYSTEM + Administrators only) |
| `...\Klinik\config\tls` | this computer's HTTPS certificate (trusted on this computer) |
| `...\Klinik\backups`, `...\Klinik\ozet` | encrypted backups, encrypted snapshots (office) |
| `...\Klinik\logs` | setup and service logs |

## 1. Office computer

Before: Windows 11 settings from office-laptop-windows.md §1 (BitLocker, never sleep, update
active hours); the office Wi-Fi/Ethernet set to **Private** network; the clinic's Google
account.

Run the .exe as an administrator:

1. **Kurulum türü**: *Ofis bilgisayarı*.
2. **Yönetici hesabı**: the owner's name and a username (e.g. `nil.yilmaz`).
3. **Google Drive**: leave ticked; a browser window opens, sign in with the clinic's Google
   account and allow access (rclone only sees the files it creates).

At the end **NIL-KLINIK-ANAHTARLAR.txt** opens on the desktop: the note key, the backup private
key, the 2FA key, the database password and the admin's temporary password.
**Print it twice, store the copies in two safe places, then delete the file and empty the
Recycle Bin.** The backup private key exists nowhere else.

The program opens at **https://localhost:8443** (desktop shortcut "Nil Psikoloji Klinik").

First day:

1. The owner logs in with the temporary password, chooses a new one, sets up 2FA (an
   authenticator app on the phone), and keeps the recovery codes.
2. Ayarlar → Odalar: add the rooms; give each therapist a default room.
3. Ayarlar → Kullanıcılar: add each therapist with the role **Terapist**. Each gets a
   temporary password (shown once) and sets a new one and 2FA at first login.
4. Each therapist: **Günlük özet** → choose a snapshot passphrase (different from the login
   password). From 17:00, `Nil Psikoloji/ozet/nil-ozet-<user>-<date>.html` appears in Drive.
5. Ayarlar → Yedekleme: the first backup appears within minutes of installing.

## 2. The owner's laptop (offline copy)

Before: **BitLocker on** (the installer warns if it is not). The laptop on the **office
network** for the installation (the first copy comes from the office).

1. On the office computer: Ayarlar → **Dizüstü kopya** → name it, choose a file passphrase
   (10+ characters) → **Eşleştirme dosyasını indir**. Copy the file to a USB stick.
2. On the laptop, run the same .exe as an administrator:
   **Kurulum türü**: *Klinik sahibinin dizüstü bilgisayarı* → choose the pairing file → type
   its passphrase.
3. The installer copies everything from the office. Log in with the **same** username,
   password and 2FA as at the office. Delete the pairing file from the USB stick.

Day to day:

- A strip under the menu says *ofisle eşit* or *ofise bağlı değil · N değişiklik bekliyor*.
- Away from the office: book, record sessions and payments, add clients as usual. Clients
  added on the laptop get numbers `DN-YYYY-9001` and up, so they never collide with the office.
- Back on the office network the laptop syncs by itself (Ayarlar → Eşitleme → *Şimdi eşitle*
  to do it at once).
- If an appointment made offline clashes with one made at the office meanwhile, the office's
  stays; the laptop's is listed in **Ayarlar → Çakışmalar** at the office. The same for a
  record edited in both places. Nothing is thrown away until the owner decides.
- Users, rooms, the therapy catalogue, backups and passwords are managed **at the office**.
- Lost laptop: at the office, Ayarlar → Dizüstü kopya → **Eşleştirmeyi iptal et**.

## Updating

Run the newer installer on each computer. It finds the existing database, keeps all data,
keys and the kind of installation, applies migrations and restarts the services. Updating
from the cloud-era version removes the old KlinikKasa/KlinikSync services; the appointments
the office already knew are kept.

## Monthly restore test (office)

With the printed backup private key typed into a file on a USB stick (`identity.txt`, one line
starting with `AGE-SECRET-KEY-`), in an administrator PowerShell:

```powershell
$k = 'C:\Program Files\Nil Psikoloji\Klinik Kasa'
& "$k\python\python.exe" -m klinik_local.service --env 'C:\ProgramData\Nil Psikoloji\Klinik\config\klinik.env' job restore-test --identity E:\identity.txt
```

Expect `RESULT: OK`. The result also appears in Ayarlar → Yedekleme. Remove the USB stick.

## Uninstalling

Settings → Apps → Nil Psikoloji Klinik. The services and the firewall rule are removed;
**data, backups and keys in ProgramData are kept** (reinstalling picks them up). Delete that
folder by hand only when the clinic no longer needs the data, after making sure a backup
exists elsewhere.

## Rebuilding after losing the office computer

1. Install on the new computer (*Ofis bilgisayarı*).
2. Stop KlinikApp and KlinikJobs. Restore the newest backup with the printed private key
   (`job restore-live --identity ... --file ... --yes`; see backups.md).
3. Run `migrate`, start the services, then make a new pairing file for the laptop and run the
   laptop installer again (its old pairing points at the old computer).
