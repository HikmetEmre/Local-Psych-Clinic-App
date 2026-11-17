# Office laptop (Windows 11 Pro): Klinik Kasa

The office laptop holds client identities and clinical notes. It must run day and night,
start everything by itself after a power cut, and be reachable only over Tailscale.

## 1. Windows settings

| Setting | Where | Value |
|---|---|---|
| Disk encryption | Settings → Privacy & security → Device encryption / BitLocker | **On**. Store the BitLocker recovery key with the offline keys (see backups.md) |
| Sleep | Settings → System → Power | Plugged in: **Never** sleep, screen off after 10 min |
| Lid | Control Panel → Power Options → Choose what closing the lid does | Plugged in: **Do nothing** |
| After power loss | BIOS/UEFI | **Power on** (if the laptop offers it) |
| Windows Update | Settings → Windows Update → Advanced → Active hours | 08:00–22:00, so restarts happen at night |
| Lock screen | Settings → Accounts → Sign-in options | Password + lock after 5 min idle |
| Account | | A dedicated local Windows account for the clinic software |

## 2. Docker that starts without anyone logging in

The brief requires the stack to start at boot **without a user login**. Docker Desktop only
starts after a login, so the recommended setup runs Docker Engine inside WSL2 Ubuntu and
starts it from Task Scheduler.

1. In an administrator PowerShell: `wsl --install -d Ubuntu-24.04`, then reboot.
2. In Ubuntu: install Docker Engine (docs.docker.com/engine/install/ubuntu), then
   `sudo usermod -aG docker $USER`.
3. Enable systemd in WSL: `/etc/wsl.conf` →
   ```
   [boot]
   systemd=true
   ```
   then `wsl --shutdown` from PowerShell. Docker now starts whenever the WSL VM starts.
4. Clone this repository inside Ubuntu (e.g. `~/klinik`) and put the env file there (§4).
5. Task Scheduler → Create Task:
   - General: *Run whether user is logged on or not*, *Run with highest privileges*.
   - Trigger: *At startup*, delay 1 minute.
   - Action: `wsl.exe -d Ubuntu-24.04 --exec bash -lc "cd ~/klinik && docker compose --env-file .env -f infra/docker-compose.yml up -d"`
   - Settings: *If the task fails, restart every 5 minutes, up to 10 times*.
6. **Test it:** restart Windows, do **not** log in, and open Klinik Kasa from a phone over
   Tailscale. Only then is this step done.

Fallback if WSL refuses to start without a session on this machine: Docker Desktop with
*Start Docker Desktop when you sign in*, plus Windows automatic sign-in (Sysinternals
Autologon, which stores the password encrypted) and a scheduled task that locks the screen at
logon. BitLocker still protects the disk. Prefer the WSL route.

## 3. Tailscale (the only way in from outside)

1. Install Tailscale for Windows, sign in with the clinic's Tailscale account.
2. In the Tailscale admin console: disable key expiry for this machine; enable MagicDNS and
   HTTPS certificates.
3. Tailscale menu → *Run unattended* (keeps it connected without a logged-in user).
4. Serve Klinik Kasa over HTTPS on the tailnet (administrator PowerShell):
   ```
   tailscale serve --bg --https=443 http://127.0.0.1:8002
   ```
   The address is `https://<machine-name>.<tailnet>.ts.net`. Use it in the office too:
   Tailscale routes directly over the LAN, and the session cookie requires HTTPS.
5. Add both therapists' phones and home computers to the tailnet. Nobody else.
6. Windows Firewall: no inbound rules are needed. The database publishes no port; the app
   listens on 127.0.0.1 only (checked by `tests/test_compose_rules.py`).

## 4. First installation

In Ubuntu, in the repository folder:

```sh
cp .env.example .env            # then fill in every value; never commit .env
docker compose --env-file .env -f infra/docker-compose.yml run --rm local-migrate \
  python -m klinik_local.manage generate-keys          # paste the output into .env
docker compose --env-file .env -f infra/docker-compose.yml up -d --build --wait
docker compose --env-file .env -f infra/docker-compose.yml run --rm -T local-migrate \
  python -m klinik_local.manage bootstrap-users - < users.toml   # same file as the cloud
```

Write the temporary passwords down and hand them over in person. At first login each
therapist sets a new password and scans the 2FA code with an authenticator app.

Copy `LOCAL_FERNET_KEYS` to the offline key escrow **before** entering any client
(see backups.md). Without it no note can ever be read again, even from a perfect backup.

## 5. Updating the software

```sh
git pull
docker compose --env-file .env -f infra/docker-compose.yml up -d --build --wait
```

Migrations run automatically (`local-migrate`). Check Ayarlar → Yedekleme and the sync
banner afterwards.

## 6. If the laptop is lost or broken

1. In Tailscale, remove the old machine. In Klinik Web (Ayarlar → Kullanıcılar) nothing
   changes; in the cloud, revoke the sync token (`UPDATE app.service_tokens SET revoked_at = now()`).
2. Set up a new laptop with §1–§3.
3. Fetch the newest backup from Google Drive into `backups/daily/`.
4. Start only the database, create the roles, restore, then start the rest:
   ```sh
   docker compose --env-file .env -f infra/docker-compose.yml up -d local-db
   docker compose --env-file .env -f infra/docker-compose.yml exec -T local-db \
     psql -U kasa_migrator -d kasa < apps/local/klinik_local/jobs/roles.sql
   # decrypt with the offline private key (USB), then:
   docker compose --env-file .env -f infra/docker-compose.yml exec -T local-db \
     pg_restore --exit-on-error --no-owner -U kasa_migrator -d kasa < backup.dump
   docker compose --env-file .env -f infra/docker-compose.yml up -d --build --wait
   ```
5. Create a new sync token in the cloud and put it in `.env`. On start the worker compares
   client codes with the cloud and refuses to reissue any code the cloud already knows;
   codes with no local client appear under Çakışmalar.
6. Everything typed in the office after the restored backup is lost (at most one hour during
   working hours). Bookings and payments are in the cloud and come back by themselves.
