# Cloud tier: Klinik Web on Supabase + Cloud Run

## 0. Go-live blocker: region (KVKK)

The cloud holds pseudonymous personal data (client codes with appointment times and
payments). Choose the Supabase and Cloud Run region **with a legal advisor** before go-live:
KVKK restricts transfers abroad. Nothing in the code depends on the region.

## 1. Supabase (database only)

Klinik Web uses Supabase only as PostgreSQL. It does not use Supabase Auth, the Data API or
client libraries. All tables live in the private schema `app`, with row-level security and no
grants for `anon` / `authenticated` (checked by `tests/integration/test_cloud_schema.py`).

1. Create the project in the chosen region. Note the **direct / session-mode** connection
   string (port 5432). The transaction pooler (6543) breaks `LISTEN`, which live updates need.
2. Settings → API: remove `app` from *Exposed schemas* if it is ever added. Leave it out.
3. Run the migrations from a trusted computer (not from Cloud Run):
   ```sh
   export CLOUD_MIGRATION_DATABASE_URL='postgresql+asyncpg://postgres:<pw>@<host>:5432/postgres'
   uv run alembic -n cloud upgrade head
   export CLOUD_APP_DB_PASSWORD='<new strong password>'
   uv run python -m klinik_cloud.manage set-role-passwords
   ```
4. Users (same `users.toml` as the office, so ids match):
   ```sh
   export CLOUD_AUTH_FERNET_KEY='<from generate-keys>'
   uv run python -m klinik_cloud.manage bootstrap-users users.toml
   ```
5. Sync token for the office worker (shown once, stored only as a hash):
   ```sh
   uv run python -m klinik_cloud.manage create-sync-token
   ```
6. Keys: `uv run python -m klinik_cloud.manage generate-keys` prints the auth key and the
   Web Push (VAPID) key pair. Set `CLOUD_VAPID_SUBJECT` to `mailto:` + the clinic's address.

## 2. Cloud Run

1. Build and push the image (`--build-arg APP=cloud`, `infra/Dockerfile`) to Artifact Registry.
2. Put the secrets in Secret Manager under the names used in `infra/cloudrun/service.yaml`.
   `CLOUD_DATABASE_URL` uses the **cloud_app** role, session mode:
   `postgresql+asyncpg://cloud_app:<pw>@<host>:5432/postgres`.
3. Fill in `<REGION>`, `<PROJECT>`, `<TAG>` and deploy:
   `gcloud run services replace infra/cloudrun/service.yaml --region <REGION>`.
4. Allow unauthenticated invocations (the app does its own login + 2FA).
5. Map a custom domain (HTTPS is automatic). Put this address in the office `.env` as
   `SYNC_CLOUD_BASE_URL` and `LOCAL_WEB_BASE_URL`.

Why these settings: minimum one instance and CPU always on, because push delivery,
reminders and the live-update listener run inside the app. With two instances each one
listens to the database, so live updates still reach every browser, and push rows are
claimed with `SKIP LOCKED`, so nothing is sent twice.

## 3. Phones (Android)

On each therapist's phone: open the Klinik Web address in **Chrome** → menu → *Add to Home
screen* → open it from the home screen → log in → Bildirimler → *Bildirimleri aç* → allow.
Test: the other therapist books a test appointment; the push arrives within seconds.

## 4. Weekly copy of the cloud database

Set `BACKUP_CLOUD_DUMP_URL` in the office `.env` to a read connection string. The office backup
job then keeps 8 weekly encrypted copies next to its own backups (and in Google Drive).
Supabase's own backups cover day-to-day recovery.
