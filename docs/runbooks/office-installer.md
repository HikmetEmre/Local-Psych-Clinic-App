# Installer: the main computer and connected computers

`NilPsikoloji-Klinik-Kurulum-<version>.exe` installs the clinic program as real Windows
services. Everything starts at boot, **before anyone logs in**.

## Decisions behind it (2026-11-18)

- **One program, two roles**: **Yönetici** (the owner: sees everything) and **Terapist**
  (whole calendar and client list, own sessions, notes and income; takes payments for any
  appointment). No cloud, no Tailscale.
- **The owner's laptop is the main computer** (ana bilgisayar): the database, the backups
  and the 17:00 snapshot live there.
- **The employee therapist's laptop is a connected computer** (bağlı bilgisayar): its own
  copy of the program, **bound to her user**. It works on its own (also when the owner is not
  in the clinic) and syncs both ways every minute whenever both laptops are on the same
  (clinic) network. It only ever receives what she may see: the calendar, the clients, her
  own sessions, notes and income. The owner's notes, the expenses and other users'
  passwords never reach it, and the main computer refuses anything she may not write.
- When the same record was changed on both, the main computer's version stays and the
  other waits in **Ayarlar → Çakışmalar** for the owner. Nothing is lost.
- **Backups**: daily at **17:00** together with the snapshot. If the owner's laptop is closed
  at 17:00, both run as soon as it is on again. Smaller backups every hour 09–21.

## What gets installed

| Service | Main computer | Connected computer |
|---|---|---|
| KlinikPostgres | PostgreSQL 16, 127.0.0.1:55432 only | same |
| KlinikApp | https://localhost:8443, and private networks (sync) | https://localhost:8443, this computer only |
| KlinikJobs | daily backup + snapshot 17:00, hourly backups | — |

| Folder | Holds |
|---|---|
| `C:\Program Files\Nil Psikoloji\Klinik Kasa` | programs (replaced on update) |
| `C:\ProgramData\Nil Psikoloji\Klinik\pgdata` | the database |
| `...\Klinik\config\klinik.env` | keys and passwords (SYSTEM + Administrators only) |
| `...\Klinik\config\tls` | this computer's HTTPS certificate (trusted on this computer) |
| `...\Klinik\backups`, `...\Klinik\ozet` | encrypted backups, encrypted snapshots (main) |
| `...\Klinik\logs` | setup and service logs |

## Before installing (both laptops)

- **BitLocker on** (Settings → Privacy & security → Device encryption). Client records are
  stored on both disks. The installer warns if it is off.
- The clinic Wi-Fi set to **Private** network on both laptops.
- Each person uses **their own Windows account**; the employee's should be a *standard*
  (not administrator) account.
- The clinic's Google account at hand (for backups and snapshots on the main computer).

## 1. Main computer (the owner's laptop)

Run the .exe as an administrator:

1. **Kurulum türü**: *Ana bilgisayar (klinik sahibi; ilk kurulum)*.
2. **Yönetici hesabı**: the owner's name and a username (e.g. `nil.yilmaz`).
3. **Google Drive**: leave ticked; a browser window opens, sign in with the clinic's Google
   account and allow access (rclone only sees the files it creates).

At the end **NIL-KLINIK-ANAHTARLAR.txt** opens on the desktop: the note key, the backup private
key, the 2FA key, the database password and the admin's temporary password.
**Print it twice, store the copies in two safe places, then delete the file and empty the
Recycle Bin.** The backup private key exists nowhere else.

First day, in the program (https://localhost:8443):

1. The owner logs in with the temporary password, chooses a new one, sets up 2FA (an
   authenticator app on the phone) and keeps the recovery codes.
2. Ayarlar → Odalar: add the rooms; give each therapist a default room.
3. Ayarlar → Kullanıcılar: add the employee with the role **Terapist**. Note her temporary
   password (shown once).
4. **The employee logs in once on the main computer** with that password, chooses her own
   password and sets up 2FA on her phone. (Passwords and 2FA are only ever changed on the
   main computer; the connected computer receives them by sync.)
5. Each therapist: **Günlük özet** → choose a snapshot passphrase.

## 2. Connected computer (the employee's laptop)

Both laptops on the clinic network, the main computer on.

1. On the main computer: Ayarlar → **Bağlı bilgisayarlar** → *Bilgisayar kimin?* = the
   employee → choose a file passphrase (10+ characters) → **Eşleştirme dosyasını indir**.
   Copy the file to a USB stick.
2. On the employee's laptop, run the same .exe as an administrator:
   **Kurulum türü**: *Bağlı bilgisayar (terapist)* → choose the pairing file → type its
   passphrase.
3. The installer copies what she may see from the main computer. She logs in with her own
   username, password and 2FA. Delete the pairing file from the USB stick.

Day to day:

- A strip under the menu says *ana bilgisayarla eşit* or *ana bilgisayara bağlı değil · N
  değişiklik bekliyor*.
- When the owner is away (main computer not in the clinic), the employee keeps working:
  bookings, sessions, notes, payments (also payments for the owner's appointments, cash,
  card or bank transfer: they count as the owner's income; the employee sees them in
  Gelir-Gider under "Diğer terapistler adına aldığınız ödemeler" for the hand-over).
  When the owner comes back, both laptops sync within a minute.
- Clients added on the employee's laptop get numbers `DN-YYYY-9001` and up.
- If an appointment made on one laptop clashes with one made on the other meanwhile, the
  main computer's stays; the other is listed in **Ayarlar → Çakışmalar**.
- The employee leaves or a laptop is lost: Ayarlar → Bağlı bilgisayarlar → **Eşleştirmeyi
  iptal et**, and deactivate the user in Ayarlar → Kullanıcılar.

## Updating

Run the newer installer on each computer (main first). It finds the existing database,
keeps all data, keys and the kind of installation, applies migrations and restarts the
services.

## Monthly restore test (main computer)

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

## Replacing the main computer (lost or broken owner's laptop)

1. Install on the new laptop (*Ana bilgisayar*).
2. Stop KlinikApp and KlinikJobs. Restore the newest backup (from Google Drive) with the
   printed private key (`job restore-live --identity ... --file ... --yes`; see backups.md).
3. Run `migrate`, start the services, make a new pairing file for the employee's laptop and
   run its installer again (its old pairing points at the old computer).
