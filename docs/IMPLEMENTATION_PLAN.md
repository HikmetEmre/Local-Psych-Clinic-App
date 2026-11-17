# Klinik App: Implementation Plan (Brief §14)

## Context

A two-therapist psychology clinic needs a shared schedule, notifications, income tracking and private clinical logs. The brief splits the system into a **cloud tier** ("Klinik Web": schedule, payments, notifications, pseudonymous `client_code` only) and a **local tier** ("Klinik Kasa": client identity and encrypted clinical records on the office laptop), joined by an outbound-only sync worker.

The repo is empty apart from `PLAN_MODE_BRIEF.md` and `TERAPILER_TAB.md` (currently at the repo root, untracked, branch `master`, no commits, no remote). This plan is the §14 deliverable. Nothing is coded yet. Precedence follows the brief: the brief wins on architecture, roles, sync and security; `TERAPILER_TAB.md` wins on the Terapiler data model and UI.

Dev machine (checked): Windows 11, Docker 29.9, Python 3.12.8, Git, WSL present. `uv` is not installed (M1 installs it).

---

## 1. Assumptions & Risks

### 1.1 Things in the specs I would change

| # | Issue | Why | What the plan does |
|---|---|---|---|
| D1 | **The §7 exclusion constraint will not create.** `starts_at + make_interval(...)` on `timestamptz` is `STABLE`, and index expressions must be `IMMUTABLE`. | Postgres rejects it at `ALTER TABLE`. A generated column fails for the same reason. | Store `ends_at timestamptz`, maintained by a `BEFORE INSERT/UPDATE` trigger from `starts_at` + `duration_min`. Constraint uses `tstzrange(starts_at, ends_at)`. |
| D2 | **Group / family / couple formats collide with "no therapist overlap" and with one appointment ↔ one session.** Five group members cannot hold five appointments at 14:00 with one therapist. | The two docs were written independently. | Phase 1 default: one appointment = one `client_code` = at most one linked session. Couple/family is one client record (the case). Extra group participants are logged as walk-in sessions. **See Q1.** |
| D3 | **Supabase Realtime for the sync pull, and an `updated_at` cursor.** | Realtime ties the worker to Supabase's client and needs a DB-level credential. A timestamp cursor silently skips rows when transactions commit out of order. | Worker talks only to the cloud FastAPI (`/sync/v1/*`) with a scoped token. Cursor is a **commit-ordered `change_seq`** (see §5). Long-poll gives near-real-time without a second mechanism. |
| D4 | **Supabase exposes `public` tables through its auto-generated Data API.** | A table without RLS is readable with the project's anon key. | All cloud tables live in a private schema `app` (not in the exposed schemas), RLS enabled with no policies for `anon`/`authenticated`, privileges revoked. The app connects with its own DB role. No Supabase Auth, no PostgREST, no Supabase client library. A CI test asserts this. |
| D5 | **Cloud Run defaults break the notification worker.** Scale-to-zero and CPU throttling outside requests stop reminders, delayed pushes and retries. `LISTEN` does not work through Supabase's transaction pooler. | | Cloud Run with `min-instances=1`, CPU always allocated, session-mode DB connection for `LISTEN`. `NOTIFY` is only a latency hint; the worker also polls the outbox every 5 s, so correctness never depends on it. Row claims use `FOR UPDATE SKIP LOCKED`, so more than one instance is safe. A small VM remains the cheaper fallback (open decision 5). |
| D6 | **"LAN allowed" conflicts with `Secure` cookies and HTTPS.** Plain `http://192.168.x.x` has no certificate. | | The local app is reached **only by its Tailscale HTTPS name**, also inside the office (Tailscale routes directly over the LAN there). The container binds to `127.0.0.1`; `tailscale serve` fronts it. No Caddy needed. |
| D7 | **TERAPILER §5 role model is superseded.** It assumes Supabase RLS with `auth.uid()`, an `admin` with full CRUD on all sessions, and a `secretary` view `v_sessions_schedule`. | Brief §5: admin does not read a colleague's records, scheduling lives in the cloud. | Local Postgres RLS keyed on `app.current_user_id`. Access = `therapist_id = current user`, or `sessions.read_others` (off by default). `v_sessions_schedule` is dropped. Soft delete is allowed for the author, not "admin". CSV export needs `admin.export` and still returns only rows the exporter may see. |
| D8 | **Local `planned` sessions and "Planlanan seans olarak oluştur" would create a second schedule.** | Only the cloud writes appointments (§3 hard rule). | The four-value status enum stays (spec is authoritative). A local `planned` session is a prepared clinical record only. The "next session" checkbox becomes a link that opens the cloud booking form prefilled (code, therapist, time). Only `completed` / `no_show` (and a revert) are pushed. |
| D9 | **`session_number = max()+1` breaks under RLS.** If both therapists have sessions with one client, each sees only their own rows and both compute the same number. | | `clients.session_counter`, incremented under a row lock on the client. Same guarantee, independent of row visibility and soft deletes. |
| D10 | **Booking by code only.** In the cloud UI, B books for `DN-2026-0042` without seeing a name. | Names never leave the office. | Kasa client pages and the Kasa calendar carry "Randevu oluştur" deep links into the cloud form, prefilled. Kasa's calendar shows names over the mirror. No local-to-cloud write proxy in Phase 1. |
| D11 | **Code format is inconsistent** (`DN-0042` in the diagram and push example, `DN-YYYY-NNNN` in §4). | | `DN-YYYY-NNNN` everywhere, validated by one shared regex and a DB `CHECK`. |

### 1.2 Risks accepted or mitigated

| # | Risk | Mitigation in plan |
|---|---|---|
| R1 | **One Fernet key for both therapists.** Separation between therapists is enforced by the app and RLS, not by cryptography. Whoever has root on the laptop can read everything. | Accepted for a two-person clinic. `MultiFernet` with key ids so rotation is possible. |
| R2 | **Losing the Fernet key loses every note**, even with perfect backups (the dump holds ciphertext). | Key is escrowed offline together with the `age` private key. Restore test decrypts a canary record. Documented in M8. |
| R3 | **Restoring an old local backup can reissue a `client_code`** that the cloud already ties to another person's appointments and payments. | On every worker start: fetch cloud `client_codes`, create placeholder clients for unknown codes, and raise the yearly counter above the cloud maximum. Tested in M5. |
| R4 | **Nightly backup = up to 24 h of notes lost** on disk failure. | Add hourly encrypted dumps during working hours (kept 48 h) next to the nightly chain. The DB is tiny. |
| R5 | **Full-disk encryption vs unattended reboot.** A power cut or night update leaves the laptop waiting for a passphrase. | Default: LUKS with TPM2 auto-unlock plus a strong OS login. Alternative noted in M8: app-level "unseal" where the Fernet key is entered after boot. Decision belongs to the clinic. |
| R6 | **Monthly restore test needs the offline `age` private key.** It cannot be a cron job. | `restore_test.sh` takes the key path as an argument. Runbook: admin plugs in the key, runs the script, removes it. |
| R7 | **Two logins per person.** Each tier has its own password and TOTP entry so each works when the other is down. | Same user UUID in both tiers, created by one bootstrap command from one file. Credentials are never synced. |
| R8 | **Web Push on iPhone works only for an installed PWA** (iOS 16.4+), and delivery time is up to Apple/Google. | Onboarding screen walks through "Add to Home Screen" then asks permission. Inbox and SSE are the reliable path. |
| R9 | **30-minute idle timeout on a phone means password + TOTP many times a day.** | Implemented as the brief says, but as a per-tier setting. Suggest relaxing it on the cloud tier after real use. SSE does not keep a session alive. |
| R10 | **Scheduler sees the payments they record.** `income.read_all = no` for B hides the reports, not the individual payments B enters for A. | Stated in the UI help and here. No stronger guarantee is possible while B records payments. |
| R11 | **Name search over Fernet ciphertext cannot use an index.** | Decrypt all client names in memory per search. Fine for a few thousand clients. Sort by code, never by name. |
| R12 | **KVKK region is unresolved.** | Go-live blocker, tracked in M8. Staging uses synthetic data only. Region is a config value, never in code. |
| R13 | **Office laptop is a single point of failure** for clinical work. | By design (brief §9 failure table). Backups, restore test and a documented rebuild procedure in M8. |

---

## 2. Final Repo Structure

```
/apps
  /cloud/klinik_cloud
    main.py  config.py  db.py  deps.py        app factory, settings, engine, require(permission)
    /auth  /takvim  /gelir  /bildirimler  /cakismalar  /ayarlar  /sync_api
        routes.py  service.py  templates/      one package per tab
    /workers  notifier.py  reminders.py        asyncio tasks started in lifespan
    /realtime  hub.py                          LISTEN connection -> per-user SSE queues
    /static  htmx, sse ext, css, sw.js, manifest (all vendored, no CDN)
    /tests
  /local/klinik_local
    main.py  config.py  db.py  deps.py
    /auth  /takvim  /danisanlar  /terapiler  /cakismalar  /ayarlar
    /crypto  fernet.py                         MultiFernet, the only place that decrypts
    /realtime  hub.py
    /static  /tests
  /sync/klinik_sync
    main.py  pull.py  push.py  reconcile.py  health.py  cloud_client.py
    /tests
/packages/shared/klinik_shared
    permissions.py   enums.py   client_code.py   sync_protocol.py (pydantic, extra=forbid)
    authkit/         bcrypt, TOTP, session cookie, CSRF, login throttle (used by both apps)
    audit.py         labels_tr.py   time.py (Europe/Istanbul helpers)
/migrations
  /cloud   alembic env + versions
  /local   alembic env + versions
/infra
  docker-compose.yml           local-app, local-db, sync-worker, backup
  docker-compose.dev.yml       adds cloud-app + cloud-db for development
  /backup    backup.sh  restore_test.sh  rclone and age notes
  /cloudrun  service.yaml  deploy notes
  /tailscale serve notes
/scripts   bootstrap_users.py  gen_keys.py (Fernet, VAPID, sync token)
/tests/e2e two-tier scenarios (compose + Playwright)
/docs      PLAN_MODE_BRIEF.md  TERAPILER_TAB.md  IMPLEMENTATION_PLAN.md  runbooks/
/.github/workflows/ci.yml
pyproject.toml (uv workspace)  .env.example  .gitignore
```

**Stack choices beyond the brief:** Python 3.12, uv workspace, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic (two chains), Jinja2 + htmx 2 + SSE extension, `bcrypt` (direct, not passlib), `pyotp`, `cryptography`, `pywebpush`, `httpx`, pytest + testcontainers, ruff, mypy, Playwright for the few end-to-end checks. CI and dev use plain Postgres 16 containers for both tiers, so Supabase is only a hosting choice.

Housekeeping in M1: move the two specs into `/docs`, rename the branch to `main`.

---

## 3. Schema per Tier

Conventions: `uuid` primary keys (client-generated accepted, for Phase 2), `timestamptz` everywhere, `version int` on mutable business rows, soft delete via `deleted_at`. Enums are `text` + `CHECK`.

### 3.1 Cloud tier (schema `app`, owner: cloud)

| Table | Key columns | Constraints / indexes |
|---|---|---|
| `users` | id, username, display_name, password_hash, totp_secret_enc, totp_last_step, default_room_id, is_active | `UNIQUE(username)`. No email column |
| `user_permissions` | user_id, permission | PK both. `CHECK` against the shared permission list |
| `auth_sessions` | id_hash, user_id, created_at, last_seen_at, totp_passed_at, revoked_at | Server-side sessions: idle timeout and revocation |
| `recovery_codes`, `login_attempts` | code_hash, used_at / username, ip, ts, success | Lockout after 5 failures |
| `service_tokens` | id, token_hash, scope (`sync`), last_used_at, revoked_at | Sync worker credential |
| `rooms` | id, internal_label `varchar(30)`, is_active, sort_order, change_seq | |
| `client_codes` | code PK, active, assigned_therapist_id, updated_at | `CHECK (code ~ '^DN-\d{4}-\d{4}$')` |
| `appointments` | id, client_code FK, therapist_id FK, room_id FK, starts_at, duration_min, ends_at, status, version, change_seq, created_by, updated_by, updated_via (`web`/`sync`), timestamps, deleted_at | `EXCLUDE` on therapist, on room, and on client_code with `tstzrange(starts_at, ends_at)`, `WHERE status IN ('planned','completed') AND deleted_at IS NULL`. `CHECK duration 10–240`. Indexes: `(therapist_id, starts_at)`, `(starts_at)`, `UNIQUE(change_seq)`, `UNIQUE(id, client_code)` |
| `payments` | id, appointment_id NOT NULL, client_code, amount `numeric(10,2)`, currency, method (`cash`/`card`/`transfer`), paid_at, recorded_by, voided_at, voided_by | Composite FK `(appointment_id, client_code)`. `CHECK amount > 0`. Corrections are void + re-enter. Indexes `(paid_at)`, `(appointment_id)` |
| `notifications` | brief's columns + `push_state`, `push_after`, `next_attempt_at`, `expires_at`, `dedupe_key` | `UNIQUE(dedupe_key)`. Partial index on pending pushes. `(recipient_id, created_at DESC)` |
| `push_subscriptions` | id, user_id, endpoint, p256dh, auth, device_kind (enum from UA), last_success_at, failure_count | `UNIQUE(endpoint)` |
| `user_settings` | user_id PK, quiet_start, quiet_end, push_enabled, reminder_24h, reminder_1h | Booleans, not JSON |
| `conflicts` | id, kind, appointment_id, author_id, proposed (typed JSON), reason_code, status, sync_item_id, resolved_by/at | |
| `sync_inbox` | item_id PK, kind, received_at, result, reason_code | Idempotency ledger |
| `audit_log` | id, ts, actor_id, actor_kind, action, entity_type, entity_id, detail (typed JSON), ip | App role has INSERT + SELECT only |

**Forbidden-data confirmation.** No name, phone, email, birth date or address of a client. No free text about a client or session. No method or diagnosis. The only human-typed strings are `users.username`, `users.display_name` (staff) and `rooms.internal_label`. JSON columns are written only through pydantic models with `extra="forbid"` whose fields are ids, enums, codes, numbers and timestamps.

The schema test (M4, runs in CI against the migrated cloud DB) has two halves:
1. **Denylist:** fails if any column name matches `name|phone|email|birth|address|note|comment|reason|descr|diagnos|method|text` outside a tiny reviewed exception list.
2. **Allowlist:** every `text`/`varchar`/`json` column must appear on a reviewed list with a length cap or a `CHECK`. A new string column fails CI until someone adds it deliberately.

### 3.2 Local tier (database `kasa`, owner: local)

| Table | Key columns | Constraints / indexes |
|---|---|---|
| `users`, `user_permissions`, `auth_sessions`, `recovery_codes`, `login_attempts` | Same shape as cloud, same user UUIDs | Separate credentials |
| `clients` | id, client_code, full_name_encrypted, phone_encrypted, email_encrypted, birth_date NULL, assigned_therapist_id, is_active, session_counter, version, timestamps, deleted_at | `UNIQUE(client_code)`. Code is immutable and never reused |
| `client_code_counters` | year PK, last_n | Row-locked allocation |
| `age_groups`, `therapy_methods`, `age_group_therapy_defaults` | Exactly as TERAPILER §3.2 | Range exclusion on age groups. Idempotent seeds from §4 |
| `therapy_sessions` | TERAPILER §3.2 columns + `appointment_id uuid NULL`, `version`, `updated_by` | `UNIQUE(client_id, session_number)`. Partial `UNIQUE(appointment_id) WHERE deleted_at IS NULL`. Kind-check trigger on `format_method_id`. Indexes from TERAPILER §3.4 |
| `session_methods` | session_id, therapy_method_id, is_primary | Kind-check trigger. Partial unique: one primary per session |
| `appointments_mirror`, `rooms_mirror` | Cloud columns + `synced_at` | Written only by the sync role. No FK to `clients` (a code may be unknown after a restore) |
| `sync_outbox` | seq bigserial, item_id UNIQUE, kind, payload, state, attempts, last_error_code, sent_at | Written in the same transaction as the business change |
| `sync_state` | key PK, value, updated_at | Cursor, last pull/push success, last error |
| `sync_conflicts` | id, outbox_item_id, kind, appointment_id, session_id, reason_code, cloud_snapshot, status, resolution | |
| `audit_log` | Same shape as cloud | Adds `notes_read`, `export` actions |

**DB roles and RLS (second layer, brief §5):**
- `kasa_migrator` owns the tables. `kasa_app` (the web app) is not the owner and is subject to RLS, with `FORCE ROW LEVEL SECURITY` on clinical tables.
- Each request transaction runs `set_config('app.current_user_id', …, true)`. Policies read it with `current_setting(..., true)`. If it is unset the policy sees NULL and returns no rows.
- `therapy_sessions` / `session_methods`: `therapist_id = current user OR has_permission(current user, 'sessions.read_others')`.
- Catalog tables: read for any logged-in user, write for `admin.catalog`. RLS is created in the same migration as each table (TERAPILER rule).
- `kasa_sync` has rights only on mirror, outbox, state and conflict tables. It cannot read `clients` or any clinical table. Outbox payloads already carry everything it sends.
- `kasa_backup` is the only role that bypasses RLS, used by `pg_dump`.

---

## 4. API Surface

Routes return HTML (full page or htmx fragment). The JSON routes from TERAPILER §7 and the sync API are thin layers over the same service functions.

### 4.1 Cloud ("Klinik Web")

| Tab | Routes | Permission |
|---|---|---|
| Auth | `GET/POST /login`, `POST /login/totp`, `POST /logout`, `/hesap/2fa`, `/hesap/sifre` | public / self |
| Takvim | `GET /takvim` (day/week, therapist, room), `GET /takvim/parca` (fragment) | `schedule.read_all` |
| | `GET /randevular/yeni` (accepts prefill query), `POST /randevular`, `PATCH /randevular/{id}` (sends `version`), `POST /randevular/{id}/iptal`, `DELETE /randevular/{id}` | `schedule.write_all` |
| | `GET /events` (SSE: `calendar`, `notification`) | any logged-in user |
| Gelir | `GET /gelir`, `GET /gelir/ozet?period=&therapist=` | `income.read_all`, else `income.read_own` narrows to self |
| | `POST /odemeler`, `POST /odemeler/{id}/iptal`, `GET /randevular/{id}/odemeler` | `payments.write` |
| Bildirimler | `GET /bildirimler`, `POST /bildirimler/{id}/okundu`, `POST /bildirimler/tumu-okundu`, `GET/PUT /bildirimler/ayarlar` | self |
| | `GET /push/anahtar`, `POST /push/abonelik`, `DELETE /push/abonelik/{id}` | self |
| Çakışmalar | `GET /cakismalar`, `POST /cakismalar/{id}/coz` | author, or `admin.users` for all |
| Ayarlar | `/ayarlar/kullanicilar` | `admin.users` |
| | `/ayarlar/odalar` | `admin.catalog` |
| | `/ayarlar/denetim`, `GET /ayarlar/disa-aktar/odemeler.csv` | `admin.export` |
| Sync API (JSON) | `GET /sync/v1/changes`, `POST /sync/v1/items`, `GET /sync/v1/client-codes`, `GET /sync/v1/snapshot`, `GET /sync/v1/ping` | service token, scope `sync` |
| PWA / ops | `/manifest.webmanifest`, `/sw.js`, `/offline`, `/healthz` | public |

### 4.2 Local ("Klinik Kasa")

| Tab | Routes | Permission |
|---|---|---|
| Auth | Same set as cloud | public / self |
| Takvim | `GET /takvim` (mirror joined to names, read-only, deep links to cloud), `GET /events` | `schedule.read_all` |
| Danışanlar | `GET /danisanlar`, `GET /danisanlar/{id}`, `GET /api/clients/search?q=` | `clients.read` |
| | `POST /danisanlar`, `PATCH /danisanlar/{id}`, `POST /danisanlar/{id}/pasif` | `clients.write` |
| Terapiler | `GET /terapiler` + fragments (tabs, table, counts, timeline) | `sessions.read_own` |
| | `GET /api/therapy/age-groups`, `/methods`, `/sessions`, `/sessions/counts`, `GET /api/clients/{id}/sessions` | `sessions.read_own` |
| | `GET /api/therapy/sessions/{id}` (decrypts, audited) | `sessions.read_own` + row ownership |
| | `POST`, `PATCH`, `DELETE /api/therapy/sessions[/{id}]` | `sessions.write_own` + row ownership |
| | `POST /api/therapy/sessions/from-appointment/{appointment_id}`, `POST /api/therapy/sessions/{id}/link-appointment` | `sessions.write_own` |
| | `GET /terapiler/disa-aktar.csv` (no notes, audited) | `admin.export` |
| Çakışmalar | `GET /cakismalar`, `POST /cakismalar/{id}/coz` | author, or `admin.users` |
| Ayarlar | `/ayarlar/kullanicilar` | `admin.users` |
| | `/ayarlar/katalog` (age groups, methods, defaults) | `admin.catalog` |
| | `/ayarlar/yedek`, `/ayarlar/esitleme` (backup and sync status) | `admin.backup` |
| | `/ayarlar/denetim` | `admin.export` |
| Ops | `/healthz` | public (loopback/tailnet only) |

Server-owned fields (`age_group_id_at_session`, `session_number`, `therapist_id`) are rejected in payloads, per TERAPILER §7.

---

## 5. Sync Protocol

**Transport.** HTTPS from the laptop to the cloud app. `Authorization: Bearer <sync token>`, header `X-Sync-Protocol: 1`. No inbound connection to the office.

### 5.1 Pull (cloud → mirror)

- **Cursor.** A trigger on `appointments` and `rooms` takes `pg_advisory_xact_lock`, then assigns `change_seq` from one shared sequence. The lock is held until commit, so sequence order equals commit order and the cursor can never skip a row. Write volume here is a few rows a minute, so serializing is free.
- **Request.** `GET /sync/v1/changes?after=1042&limit=500&wait=25`. The server answers at once if rows exist, otherwise holds the request up to `wait` seconds and wakes on `NOTIFY`. If long-poll fails, the worker falls back to a 30 s poll.
- **Response.**
  ```json
  {"changes":[{"seq":1043,"entity":"appointment","data":{"id":"…","client_code":"DN-2026-0042",
     "therapist_id":"…","room_id":"…","starts_at":"…","duration_min":50,"status":"planned",
     "version":3,"deleted_at":null,"updated_at":"…"}}],
   "next_cursor":1043,"has_more":false}
  ```
- **Apply.** Upsert into the mirror only if `incoming.version > mirror.version`. The cursor update is in the same local transaction, then `NOTIFY mirror_changed` refreshes open Kasa calendars over SSE.
- **Safety net.** Nightly and at every start: `GET /sync/v1/snapshot` (last 90 days + future), compared against the mirror and repaired.

### 5.2 Push (local outbox → cloud)

```json
POST /sync/v1/items
{"items":[
  {"item_id":"uuid","seq":17,"kind":"client_code.upsert",
   "payload":{"code":"DN-2026-0042","active":true,"assigned_therapist_id":"uuid"}},
  {"item_id":"uuid","seq":18,"kind":"appointment.outcome",
   "payload":{"appointment_id":"uuid","new_status":"completed","prev_status":"planned",
              "actor_id":"uuid","recorded_at":"…"}},
  {"item_id":"uuid","seq":19,"kind":"conflict.resolved",
   "payload":{"conflict_item_id":"uuid","resolution":"discarded"}}
]}
→ {"results":[{"item_id":"…","result":"applied|duplicate|rejected",
               "reason_code":"status_mismatch|unknown_appointment|appointment_deleted|overlap|unknown_therapist|invalid",
               "current":{…appointment snapshot…}}]}
```

- **Order.** Items go in `seq` order, up to 50 per call. The cloud handles each item in its own transaction.
- **Idempotency.** The cloud inserts `item_id` into `sync_inbox` in the same transaction as the change. A repeat returns `duplicate` with the stored result.
- **Outcome rule.** Status is a compare-and-set on the status field, not on `version`: accepted when the cloud status equals `prev_status` (or already equals `new_status`). So a rescheduled appointment still accepts its outcome, while a cancelled one does not. Accepted outcomes bump `version` and `change_seq` and write the normal notification rows.
- **Revert.** Deleting or reopening a linked session pushes `new_status: "planned"` with `prev_status` set to the outcome it replaces.

### 5.3 Conflict flow

1. Cloud rejects an item: it writes a cloud `conflicts` row and `conflict.created` notifications (author + admin), and returns `rejected` with the current snapshot.
2. Worker marks the outbox item `rejected`, writes `sync_conflicts`, and holds later items for the same appointment. Other items continue.
3. Çakışmalar shows both sides. The author picks **discard mine** or **apply mine anyway** (a new item whose `prev_status` is the current cloud status).
4. The resolution is pushed as `conflict.resolved`, which closes the cloud row. Nothing is dropped silently.

Booking conflicts in the cloud (the 409 from optimistic locking) are stored in the same cloud `conflicts` table with the rejected field values, and resolved in the cloud tab as "keep theirs" or "re-apply mine on the current version". An overlap rejection (exclusion constraint) is a plain form error, not a conflict row.

### 5.4 Error states

| State | Worker behaviour | What the user sees |
|---|---|---|
| Cloud unreachable / 5xx | Retry from the first unacknowledged item, backoff up to 5 min. Pull retries independently | Kasa banner after 5 min stale; calendar marked read-only with last sync time |
| 401 (token revoked) | Stop both loops, no retry storm | Banner: sync credential invalid |
| 426 (protocol mismatch) | Stop | Banner: update required |
| Item rejected | Conflict flow above | Çakışmalar entry + notification |
| Same item fails 10 times with 5xx | Mark `dead`, raise a `sync_error` conflict, continue with other appointments | Çakışmalar entry |
| Local DB restored from backup | Start-up reconcile: codes (R3), cursor re-pull, outbox re-send (all idempotent) | Placeholder clients flagged for re-entry |

Health: the worker writes heartbeats to `sync_state` (the Kasa banner reads the table) and serves `/healthz` for the Docker healthcheck.

---

## 6. Notification Pipeline

1. **Outbox write.** The appointment service computes recipients (brief §8.3: assigned therapist and every holder of `schedule.write_all`, minus the actor) and inserts one `notifications` row each **in the same transaction** as the appointment change. `push_after` is set at insert time: now, or the end of the recipient's quiet hours in `Europe/Istanbul`. The transaction ends with `NOTIFY new_notification` and `NOTIFY appointment_changed`.
2. **Payload.** Typed JSON only: `appointment_id`, `client_code`, `starts_at`, `therapist_id`, `changed_fields`, `actor_id`. Text is rendered from templates at delivery time, e.g. `🗓 Yeni randevu: 12.11 Perş 14:00 · DN-2026-0042`.
3. **SSE.** One dedicated `LISTEN` connection per app instance feeds an in-process hub with a queue per connected user. `notification` events update the bell; `calendar` events tell htmx to refetch the affected day or week, for every user including the actor's other tabs. Reconnects use `Last-Event-ID`; a heartbeat comment goes out every 20 s.
4. **Web Push worker.** Wakes on `NOTIFY` and every 5 s. Claims rows where `push_state='pending'` and `coalesce(next_attempt_at, push_after) <= now()` using `FOR UPDATE SKIP LOCKED`. Sends to each of the recipient's subscriptions through `pywebpush` (payload is end-to-end encrypted to the browser).
   - Success: `pushed_at`, `push_state='sent'`.
   - 404/410: delete that subscription.
   - Other failure: `push_attempts += 1`, backoff 30 s, 2 min, 10 min, 1 h, then `failed` after 5 attempts. The inbox row stays either way.
   - Skipped: row already read, `expires_at` passed, user disabled push, or no subscriptions.
5. **Quiet hours.** The row is in the inbox and on SSE at once; only the push waits for `push_after`. Pushes use a per-appointment `tag`, so several delayed changes to one appointment collapse on the device.
6. **Reminders.** A once-a-minute task inserts `appointment.reminder_24h` when now is within `[start-24h, start-23h]` and `_1h` within `[start-60m, start-45m]`, for `planned` appointments. `dedupe_key = reminder:{id}:{kind}:{starts_at}` makes it idempotent and re-arms after a reschedule. `expires_at = starts_at`.
7. **Service worker.** Handles `push` and `notificationclick` (opens the calendar on that day) and caches the last fetched week for read-only offline viewing. The cache is cleared on logout.

---

## 7. Milestones and 8. Tests

Each milestone ends green in CI and is committed separately (larger ones as several commits). Tests run against real Postgres 16 containers.

### M1: Skeleton, compose, CI, shared package
- **Build:** uv workspace, the tree from §2, both Alembic chains with an empty baseline, dev compose (two DBs, three services with `/healthz`), `.env.example`, `.gitignore`, GitHub Actions (ruff, mypy, pytest). Shared: permission constants, enums, `client_code` parse/format/validate, sync protocol models, time helpers. Specs moved to `/docs`.
- **Tests:** unit tests for `client_code` and protocol models (reject unknown fields); compose smoke test hits all `/healthz`; CI runs both migration chains up and down.

### M2: Local auth, permissions, clients, audit
- **Build:** `authkit` (bcrypt, TOTP with replay guard, recovery codes, server-side sessions, 30-min idle, CSRF, lockout), `require(permission)`, DB roles and the per-transaction user setting, `scripts/bootstrap_users.py`, Fernet service (`MultiFernet`), Danışanlar CRUD with code allocation, `sync_outbox` rows for `client_code.upsert`, `audit_log`, user admin page.
- **Tests:** login/TOTP/lockout/idle-timeout; permission matrix for A and B on every route; concurrent client creation yields distinct consecutive codes; stored name bytes ≠ plaintext; audit rows for login and client changes with no decrypted text; outbox row is written in the same transaction (rollback test).

### M3: Local Terapiler tab
- **Build:** follows TERAPILER §8 as separate commits: catalog migration + RLS + kind triggers → idempotent seed → sessions migration + indexes + RLS → services (`resolve_age_group`, counter-based session numbers, sanitizer, audit) → JSON API → read UI (tabs, chips, filters, table, counts, URL state) → write modal → timeline + detail drawer → CSV export. With the D7, D8, D9 adjustments.
- **Tests:** every item in TERAPILER §9. RLS test adapted: connect as `kasa_app` with user A's id set and prove B's sessions are invisible **at the SQL level** and through every route (list, detail, patch, delete, timeline, counts, export). Unset user id returns nothing. Age boundaries 12/13, 17/18, 64/65 and birthday-on-session-date. Two concurrent inserts for one client give 7 and 8. Detail read writes `notes_read`; list never contains note text.

### M4: Cloud auth, appointments, overlap and version rules
- **Build:** cloud migrations in schema `app` with the D4 lockdown, auth via `authkit`, rooms, `client_codes`, appointments with `ends_at` trigger, three exclusion constraints, `change_seq` trigger, optimistic locking with 409 + conflict row, Takvim day/week UI with create/edit/cancel, cloud audit, user and room admin. Deploy a staging instance (synthetic data) to surface Cloud Run and Supabase issues early.
- **Tests:** **forbidden-columns test** (both halves, §3.1) and the Data API lockdown test; 20 parallel bookings for one therapist/slot → exactly one succeeds, the rest get a clear Turkish error; same for room; adjacent slots (14:00–14:50, 14:50–15:40) both succeed; cancelled slot can be rebooked; stale `version` → 409 with current record, conflict row created; different-field merge; permission matrix; times render in Europe/Istanbul.

### M5: Sync worker
- **Build:** `/sync/v1/*` on the cloud with service token and `sync_inbox`; worker pull (long-poll + fallback), push, start-up and nightly reconcile, heartbeats; local mirror tables; Kasa Takvim with names and deep links; "Seansı başlat/kaydet" from a mirrored appointment; link-appointment for walk-ins; outcome push and revert; Çakışmalar in both tiers; stale-sync banner.
- **Tests:** replay the same batch 3 times → state unchanged, results `duplicate`; kill the worker between send and ack → no double apply; **cloud down:** local notes work, calendar read-only, outcomes queue, then drain in order on recovery; **office down:** cloud bookings continue, mirror catches up from the cursor with no manual step; outcome on a cancelled appointment → conflict on both sides, both resolutions work; outcome on a rescheduled appointment is accepted; restore-from-old-backup scenario → no code is reissued, placeholders created; out-of-order commit test proves the cursor skips nothing; `kasa_sync` cannot `SELECT` from `clients` or `therapy_sessions`; captured sync traffic contains no name or note text.

### M6: Notifications
- **Build:** outbox writes inside appointment transactions, recipient rule, SSE hub on both tiers, bell + inbox + read state, PWA manifest and service worker, VAPID keys, subscription management, push worker with retry and quiet hours, reminders, notification settings, read-only week cache.
- **Tests:** recipients table from brief §8.3 incl. "never the actor"; rollback of the appointment leaves no notification; fake push server returning 201/410/500 → sent, subscription removed, backoff then `failed` after 5; quiet-hours boundaries (21:59, 22:00, 07:59, 08:00, across midnight); reminder idempotency and re-arm on reschedule; payloads contain only the typed fields; Playwright: B books for A in one browser, A's open calendar and bell update without reload; offline page shows the cached week.

### M7: Income tab
- **Build:** payments against appointments (several per appointment, void instead of edit), Gelir totals by day/week/month and by therapist, permission-scoped queries, payments CSV for admin, audit on every payment change.
- **Tests:** B gets own totals only and a 403 on clinic totals through every route and query parameter; A sees both; voided payments excluded; month and week boundaries in Europe/Istanbul; decimal sums exact; payment without an appointment is rejected; walk-in path (create appointment, link session, pay) end to end.

### M8: Backups, restore test, deployment docs, hardening
- **Build:** `backup` service (nightly + hourly `pg_dump` → `age` → `rclone`, retention 30 daily / 12 monthly / 48 h hourly), weekly cloud dump, `restore_test.sh`, backup status page, production compose (`restart: unless-stopped`, DB port unpublished, app on loopback), Tailscale serve, Cloud Run config, runbooks (laptop setup, power settings, key escrow, user bootstrap, restore, laptop rebuild, token rotation), security headers and CSP, rate limits, dependency audit in CI.
- **Tests:** `restore_test.sh` restores into a throwaway container, checks row counts and decrypts a canary note; backup file is not readable without the private key; port scan of the compose host shows no Postgres; every item in brief §16 run as a scripted checklist.
- **Go-live blockers tracked here:** KVKK region decision (open decision 4), FDE/reboot choice (R5), key escrow done (R2).

---

## 9. Questions for the Developer

None of these stop M1–M2. The defaults below are what gets built unless you say otherwise.

| # | Question | Default if unanswered | Needed by |
|---|---|---|---|
| Q1 | Does the clinic run **group sessions** with several separately-recorded clients, and how should couple/family cases be booked? | One appointment = one client code. Couple/family = one client record. Group participants beyond the first are walk-in sessions (D2) | M3 schema, M4 constraint |
| Q2 | Is **code-only booking in the cloud with deep links from Kasa** acceptable for B's daily secretary work (D10)? | Yes | M4/M5 UI |
| Q3 | **Office laptop OS**: native Ubuntu, or Windows + WSL2? | Ubuntu | M8 |
| Q4 | Which **phones** do A and B use (iPhone needs the installed-PWA flow)? | Support both | M6 onboarding |
| Q5 | Should a therapist be allowed to **soft-delete their own session**, given admin cannot see it (D7)? | Yes, audited | M3 |

---

### Answers received (2026-11-16)

| # | Decision |
|---|---|
| Q1 | Default accepted: one appointment = one client code; couple/family = one client record; extra group participants are walk-in sessions. |
| Q2 | Default accepted: code-only booking in the cloud, prefilled deep links from Kasa. |
| Q3 | **Office laptop is Windows** (Docker Desktop + WSL2). R5 becomes BitLocker; M8 runbooks, power settings and auto-start are written for Windows. |
| Q4 | **Android only** (admin uses a Samsung). No iPhone onboarding flow is built. |
| Q5 | Yes: a therapist may soft-delete their own session (audited). |

**Branding:** the clinic is Nil Psikoloji. The logo is `assets/nil_psikoloji_logo.jpeg`; both apps use a white / warm beige / deep green theme derived from it.

---

## Verification (of the build, once approved)

- Per milestone: `uv run pytest` for the touched packages plus the integration suite against compose (`docker compose -f infra/docker-compose.dev.yml up`), all green in CI before the milestone commit.
- Cross-tier: `tests/e2e` brings up both tiers and the worker, then runs the cloud-down, office-down, replay and conflict scenarios from M5 and the Playwright two-browser check from M6.
- Final: the six Definition-of-Done items in brief §16, run as a scripted checklist in M8 against the staging cloud instance and the real office laptop.

## First step after approval

Start M1. Its first commit moves the two specs into `/docs` and saves this plan beside them as `docs/IMPLEMENTATION_PLAN.md`.


---

## Status (2026-11-17)

M1–M8 are built and committed, one commit (or a few) per milestone. Test suite: unit,
integration on real PostgreSQL 16, two-database sync scenarios, real backup/restore, and
an opt-in browser test against the dev stack.

### Where the build differs from the plan

| Plan | Built | Why |
|---|---|---|
| Sync `GET /snapshot` + nightly repair | Not built. The pull is cursor-only; client codes are reconciled at every worker start | The commit-ordered `change_seq` makes the cursor complete; every row change carries a new number. Add the snapshot if a mirror is ever seen to drift |
| Kasa → Klinik Web deep links from client pages | One "Randevu oluştur (Klinik Web)" button on the Kasa calendar; Klinik Web's booking form accepts `?kod=`, `?terapist=`, `?baslangic=` | Small follow-up: add the prefilled link on the client page |
| Column visibility toggle per user (TERAPILER_TAB §6.4) | Not built | Needs a small client-side script; low value for two users |
| iPhone onboarding | Not built | Decision Q4: Android only |
| R5 (disk encryption) | BitLocker, per decision Q3 (Windows) | — |
| Office laptop autostart | WSL2 + Docker Engine + Task Scheduler (recommended), Docker Desktop + autologon (fallback) | Docker Desktop alone cannot start before a login |

### Found and fixed by testing against real systems

- Backup pruning of an empty folder made every backup report failure (`pipefail`).
- Restoring into a new server failed: server-wide roles are not in a dump (`apps/local/klinik_local/jobs/roles.sql`).
- Forced password page reloaded itself in a loop (background bell request redirected to it).
- Pages scrolled sideways on phones (absolutely positioned hidden inputs escaped their box).
- The web app had write access to the whole outbox; narrowed to one function.

### Not yet done

- CI has never run: the repository has no remote. The workflow is in `.github/workflows/ci.yml`.
- Real deployment: Supabase/Cloud Run project, region decision (KVKK), office laptop setup,
  real push to a phone. All described in `docs/runbooks/`.
