# Klinik App: Plan Mode Brief

> **How to use:** Start Claude Code in plan mode and say:
> *"Read `PLAN_MODE_BRIEF.md` and `TERAPILER_TAB.md`. Produce the implementation plan described in Section 14. Do not write code yet."*
>
> **Precedence:** This brief is the **source of truth for architecture, stack, roles, sync and security.** `TERAPILER_TAB.md` stays authoritative for the Terapiler tab's **data model and UI**. Where it mentions Supabase RLS for clinical tables, replace that with the local-tier rules in this brief.

---

## 1. Product Summary

A management app for a **small psychology clinic with exactly two therapists**.

| User | Role | Responsibilities |
|---|---|---|
| Therapist A | `admin` + `therapist` | Runs own sessions, manages the system (users, catalog, backups, exports) |
| Therapist B | `therapist` + `scheduler` | Runs own sessions **and** does secretary work: booking appointments for both therapists, recording payments |

Core needs:
1. Shared **schedule** reachable from anywhere (phone, home, office).
2. **Notifications**: when one person creates/changes/cancels an appointment, the other is notified, even if their app is closed.
3. Each therapist's **therapy logs and session notes** stay private, encrypted, and **physically in the office**, yet reachable and editable from home.
4. **Income** tracking.
5. Must keep working when either side (cloud or office) is temporarily unavailable, for the data that side owns.

---

## 2. Architecture: Hybrid, Two Tiers

The guiding principle: **split data by sensitivity, and give every piece of data exactly one home (one writer location).**

```
            ┌──────────── CLOUD TIER: "Klinik Web" ─────────────┐
 Phone /    │  Appointments · Schedule · Payments · Rooms       │
 anywhere ─►│  Notifications · Push subscriptions               │
 HTTPS+2FA  │  Patients appear ONLY as client_code (DN-0042)    │
            └───────────────▲──────────────────┬────────────────┘
                            │ push: status     │ pull: appointment
                            │ only             ▼ changes
            ┌──────── LOCAL TIER: "Klinik Kasa" (office laptop) ────┐
 Tailscale ►│  Client identity (name, contact, birth date)          │
 (home) or  │  Therapy sessions/logs · Notes · Homework (Fernet)    │
 LAN        │  Appointments mirror (read-only) · Sync worker        │
            └───────────────────────────┬───────────────────────────┘
                                        │ nightly encrypted backup
                                        ▼
                                  Google Drive
```

### 2.1 Stack (defaults; plan may challenge them with reasons)

| Concern | Cloud tier | Local tier |
|---|---|---|
| Database | Supabase PostgreSQL (EU region, see §12) | PostgreSQL 16 in Docker |
| Backend | FastAPI | FastAPI |
| Frontend | Server-rendered Jinja2 + HTMX (+ SSE extension) | Same |
| Hosting | Google Cloud Run (or a small VM) | `docker compose` on the office laptop |
| Network access | Public HTTPS, login + TOTP 2FA | **Tailscale only**. No public ports. LAN allowed |
| Auth | Own user table, bcrypt + TOTP | Own user table, bcrypt + TOTP (works when the cloud is down) |
| Real-time | Postgres `LISTEN/NOTIFY` → SSE | Same, for mirror updates |
| Push | Web Push (VAPID), app installed as PWA | n/a |

**Repo layout:** a single monorepo.
```
/apps/cloud      FastAPI app for Klinik Web
/apps/local      FastAPI app for Klinik Kasa
/apps/sync       Sync worker (runs on the local laptop)
/packages/shared Pydantic schemas, enums, permission constants, client_code utils
/infra           docker-compose.yml, Caddy/Tailscale notes, backup scripts, Cloud Run config
/migrations      /cloud and /local (separate migration chains)
/docs            PLAN_MODE_BRIEF.md, TERAPILER_TAB.md
```

---

## 3. Data Ownership: HARD RULES

These rules are non-negotiable. Every table belongs to exactly one tier. Only the owner tier writes it.

| Data | Owner (writer) | Copy in other tier | Who edits |
|---|---|---|---|
| `appointments` (time, therapist, room, client_code, status) | ☁ Cloud | Local: `appointments_mirror` (read-only) | admin, scheduler |
| `payments` (amount, method, date, client_code, appointment_id) | ☁ Cloud | ❌ | admin, scheduler |
| `rooms` | ☁ Cloud | Local mirror | admin |
| `notifications`, `push_subscriptions`, `user_settings` | ☁ Cloud | ❌ | system / self |
| `clients` (name, phone, email, birth_date, client_code) | 🏠 Local | ❌ **never leaves the office** | both therapists |
| `therapy_sessions`, `session_methods`, notes, homework | 🏠 Local | ❌ | author therapist only |
| Therapy catalog (`age_groups`, `therapy_methods`, defaults) | 🏠 Local | ❌ | admin |
| Session outcome (`completed` / `no_show`) | 🏠 Local (set on the therapy session) | Pushed to cloud as **status only** | treating therapist |

**Forbidden in the cloud tier, enforced by schema review and tests:**
- Client names, phone numbers, emails, birth dates, addresses.
- Any free-text field about a client or session ("reason", "notes", "comment"). People type sensitive details into any free-text box. The only exception is a short `internal_label` on rooms.
- Therapy method or diagnosis information.

**Why:** a calendar entry in a psychology clinic is itself personal data. The cloud should hold only what the schedule and the cash book need, keyed by a pseudonymous code.

---

## 4. Identity & the `client_code` Link

- `client_code` format: `DN-YYYY-NNNN` (e.g. `DN-2026-0042`), generated **only in the local tier** when a client is created.
- The cloud tier knows codes, never identities.
- The **local** app shows schedules with real names by joining `appointments_mirror.client_code` → `clients.client_code`.
- The **cloud** web (e.g. on a phone away from Tailscale) shows codes only.
- Creating an appointment in the cloud requires an existing `client_code`. The cloud keeps a minimal `client_codes` table (code, active flag, assigned_therapist_id), and the sync worker pushes only those three fields from local.

---

## 5. Roles & Permissions

Users hold **sets of permissions**, not one role. B is not a special "therapist-secretary" role: B has both permission sets. If a real secretary joins later, they get only the scheduler set, with no code change.

| Permission | Admin (A) | Therapist + scheduler (B) |
|---|:-:|:-:|
| `schedule.read_all` | ✅ | ✅ |
| `schedule.write_all` (create/edit/cancel for anyone) | ✅ | ✅ |
| `payments.write` | ✅ | ✅ |
| `income.read_all` (clinic total + per therapist) | ✅ | ❌ ⚙ |
| `income.read_own` | ✅ | ✅ |
| `clients.read` / `clients.write` | ✅ | ✅ |
| `sessions.read_own` / `sessions.write_own` (notes, logs) | ✅ | ✅ |
| `sessions.read_others` (other therapist's notes) | ❌ ⚙ | ❌ |
| `admin.users`, `admin.catalog`, `admin.export`, `admin.backup` | ✅ | ❌ |

⚙ = **default chosen in this brief, configurable.** Admin manages the system, not a colleague's clinical records.

Implementation: `user_permissions(user_id, permission)` in each tier; FastAPI dependency `require(permission)`; every clinical query also filters `therapist_id = current_user.id`. Also enable Postgres RLS on clinical tables in the local DB as a second layer, setting `app.current_user_id` per transaction.

---

## 6. Modules (Tabs)

The app follows a tab-based structure. Each tab owns its routes, templates and service layer.

| Tab | Tier | Summary |
|---|---|---|
| **Takvim** (Schedule) | ☁ + 🏠 view | Day/week view per therapist and room. Create/edit/cancel. Real-time updates via SSE. Local view shows names, cloud view shows codes |
| **Danışanlar** (Clients) | 🏠 | Client CRUD, code generation, age group derived from birth date |
| **Terapiler** | 🏠 | Session log table split by age group with default therapy types. **Full spec in `TERAPILER_TAB.md`** |
| **Gelir** (Income) | ☁ | Record payments against appointments. Totals by day/week/month, by therapist (permission-filtered) |
| **Bildirimler** | ☁ | Inbox (🔔) with read/unread, notification settings, quiet hours |
| **Çakışmalar** (Conflicts) | ☁ + 🏠 | List of sync/booking conflicts needing a human decision (see §9) |
| **Ayarlar** (Admin) | both | Users, permissions, rooms, therapy catalog, backup status |

Appointment ↔ therapy session relationship:
- An **appointment** (cloud) is the booking.
- A **therapy session** (local) is the clinical record. It is created from a mirrored appointment when the therapist opens it ("Seansı başlat/kaydet"), linked by `appointment_id`.
- Walk-in sessions without an appointment are allowed. They create a local session only, and the cloud receives nothing unless a payment is recorded (which then requires creating an appointment record first).

---

## 7. Appointment Logic (Cloud)

- Fields: `id (uuid)`, `client_code`, `therapist_id`, `room_id`, `starts_at`, `duration_min`, `status` (`planned`, `completed`, `cancelled`, `no_show`), `version int`, `created_by`, `updated_by`, timestamps, `deleted_at`.
- **No double-booking, enforced in the database:**
  ```sql
  CREATE EXTENSION IF NOT EXISTS btree_gist;
  ALTER TABLE appointments ADD CONSTRAINT no_overlap_therapist EXCLUDE USING gist (
    therapist_id WITH =,
    tstzrange(starts_at, starts_at + make_interval(mins => duration_min)) WITH &&
  ) WHERE (status IN ('planned','completed') AND deleted_at IS NULL);
  ```
  Add the same constraint on `room_id`. ⚙ Default: **separate rooms**, so the room constraint exists but each therapist has a default room. If they share one room, the constraint already handles it.
- **Optimistic locking:** every update sends the `version` it read. `UPDATE … WHERE id=$1 AND version=$2`, then `version = version + 1`. If 0 rows are updated, return 409 with the current record, and the UI shows *"Bu randevu az önce değiştirildi."* with options to reload or open the conflict view.
- Same-field conflicts are never resolved automatically. Different-field edits may be merged server-side.
- Times are stored as `timestamptz`. Display in `Europe/Istanbul`.

---

## 8. Notifications

### 8.1 Outbox pattern
Every appointment write inserts a `notifications` row **in the same transaction**. A notification can never be lost, and the table doubles as the in-app inbox.

`notifications(id, recipient_id, event_type, appointment_id, payload_json, created_at, read_at, pushed_at, push_attempts)`

### 8.2 Delivery
- `NOTIFY new_notification` → cloud worker → **SSE** to open browser tabs (calendar and bell update live) **and Web Push** to the recipient's subscribed devices.
- Retry Web Push with backoff (max 5 attempts). Remove subscriptions that return 404/410.
- Quiet hours per user (default 22:00–08:00): the notification goes to the inbox, and the push is delayed until quiet hours end.

### 8.3 Who gets notified
**Rule: notify everyone affected, never the person who made the change.**

| Event | Recipients |
|---|---|
| `appointment.created/updated/cancelled` for therapist X | X (unless X is the actor) + all users with `schedule.write_all` (except the actor) |
| `appointment.reminder_24h` / `_1h` | assigned therapist |
| `conflict.created` | the author of the rejected change + admin |

### 8.4 Payload privacy
Push text never contains a client name or anything clinical:
> `🗓 Yeni randevu: 12.11 Perş 14:00 · DN-0042`

---

## 9. Sync Worker (runs on the office laptop)

- Connections are **outbound only**, from the laptop to the cloud. No inbound ports are opened in the office.
- **Pull:** subscribe to cloud appointment/room changes (Supabase Realtime, falling back to polling every 30 s on `updated_at > last_cursor`). Upsert into `appointments_mirror` / `rooms_mirror`. Store the cursor in `sync_state`.
- **Push:**
  - `client_codes` (code, active, assigned_therapist_id) when a client is created or changed locally.
  - Appointment outcome status (`completed` / `no_show`) when a therapist records it locally.
- Push uses a **local outbox** (`sync_outbox`) written in the same local transaction. The worker sends items in order and marks them done. It is idempotent: each item carries a UUID, and the cloud ignores duplicates.
- If the cloud rejects an item (version mismatch, unknown appointment), it goes into a `conflicts` row and shows in the **Çakışmalar** tab. Never drop it silently.
- Health: the worker exposes `/healthz` with last successful pull/push time. The local UI shows a banner if sync has been stale for more than 5 min.

### Failure behavior

| Down | Still works | Waits |
|---|---|---|
| Office laptop / office internet | Schedule, payments, notifications (cloud) | Notes and logs until the laptop is back |
| Cloud | Notes, logs, clients, **read-only** schedule from the mirror | New bookings and payments. Local status pushes queue in the outbox |
| Home internet | Office user unaffected | Home user offline (Phase 2 covers drafts) |

---

## 10. Offline (Phased)

- **Phase 1 (build now):** online only. The PWA caches the last fetched week of schedule for **read-only** offline viewing (codes only, from the cloud).
- **Phase 2 (later, separate plan):** offline queue for session-note **drafts** and tentative appointments. Requirements: device-side encryption, unlock with login, delete after server confirmation, conflicts go to Çakışmalar. **Do not implement in Phase 1.** Only keep the design compatible: client-generated UUIDs, `version` columns, outbox pattern.

---

## 11. Security

| Layer | Requirement |
|---|---|
| Transport | HTTPS everywhere (Cloud Run TLS; `tailscale serve` certificate locally) |
| Local network | Postgres port never exposed beyond the compose network. App reachable via LAN + Tailscale only |
| Auth | bcrypt, TOTP 2FA required, session cookies `HttpOnly; Secure; SameSite=Lax`, idle timeout 30 min, CSRF tokens on forms |
| Clinical text | Fernet encryption in the backend service layer (notes, homework, client contact info). Key loaded from an env file outside the repo, never stored in the DB |
| Disk | Full-disk encryption on the office laptop |
| Audit | `audit_log` in both tiers: logins, appointment changes, payment changes, **every decrypt of notes**, exports. No secrets or decrypted text in logs |
| Least data | Cloud schema review test that fails if forbidden columns (§3) appear |
| Secrets | `.env` files git-ignored. A `.env.example` committed. Sync worker uses a dedicated, limited cloud credential |

---

## 12. Deployment & Operations

### Office laptop (local tier)
- OS: Ubuntu (native preferred; if Windows + WSL2, Docker must start at boot without a user login).
- Power: never sleep, ignore lid close, OS updates scheduled at night.
- `docker compose` services: `local-app`, `local-db`, `sync-worker`, `backup` (cron). All `restart: unless-stopped`.
- Tailscale on the host, `tailscale serve` → local-app.

### Backups
- Nightly `pg_dump` (local DB) → encrypt with `age` (public key on server, **private key kept offline**) → `rclone` to Google Drive. Keep 30 daily and 12 monthly.
- Supabase's own backups cover the cloud tier, plus a weekly encrypted `pg_dump` of the cloud DB to Drive.
- `scripts/restore_test.sh` restores the latest backup into a throwaway container and runs sanity queries. Run monthly. Document it.

### Data location
⚙ Choose a Supabase/Cloud Run region before go-live. KVKK has rules on cross-border transfer, and the cloud tier still holds personal data even though it is pseudonymous. Flag this as a **go-live blocker** to confirm with a legal advisor. Don't decide it in code.

---

## 13. Open Decisions (defaults applied, keep them configurable)

| # | Question | Default in this brief |
|---|---|---|
| 1 | Shared room or separate rooms? | Separate rooms, room constraint still active |
| 2 | Can admin read the other therapist's notes? | No |
| 3 | Income visibility | Admin: clinic total + per therapist. B: own only |
| 4 | Cloud region (KVKK) | Unresolved. Go-live blocker |
| 5 | Cloud hosting: Cloud Run vs small VM | Cloud Run |

---

## 14. What the Plan Must Contain

Produce a plan, not code. Include:

1. **Assumptions & risks:** anything in this brief you disagree with, with reasons and alternatives.
2. **Final repo structure** (refine §2.1).
3. **Schema per tier:** table list with key columns, constraints, indexes; mark owner tier per table; confirm no forbidden data in the cloud.
4. **API surface per app:** routes grouped by tab, with the permission each requires.
5. **Sync protocol:** message shapes, cursor handling, idempotency, conflict flow, error states.
6. **Notification pipeline:** outbox → worker → SSE/Web Push, retry and quiet-hours logic.
7. **Milestones**, each independently testable and committed separately. Suggested order:
   - M1 Repo skeleton, compose, CI (lint + tests), shared package
   - M2 Local: auth + permissions + clients + audit
   - M3 Local: Terapiler tab (per `TERAPILER_TAB.md`)
   - M4 Cloud: auth + appointments + overlap/version rules
   - M5 Sync worker (pull mirror, push codes/status, conflicts)
   - M6 Notifications (inbox, SSE, Web Push, quiet hours)
   - M7 Income tab
   - M8 Backups, restore test, deployment docs, hardening
8. **Test strategy per milestone:** unit, integration (two-user permission tests, concurrent booking, sync replay/idempotency, cloud-down/office-down scenarios), and the forbidden-columns schema test.
9. **Questions for the developer:** only ones that block the plan.

## 15. Out of Scope

- Offline writes (Phase 2), invoicing/e-Fatura, SMS/WhatsApp reminders to clients, client self-booking portal, multi-clinic support, mobile native apps.

## 16. Definition of Done (whole project)

- [ ] Therapist B books an appointment for A from home. A's phone gets a push within seconds, and A's open calendar updates without reload.
- [ ] Two simultaneous bookings for the same therapist/time: exactly one succeeds, and the other gets a clear error.
- [ ] Laptop offline: schedule and payments still work in the cloud. When it comes back, the mirror catches up with no manual action.
- [ ] Cloud offline: notes and logs still work locally. Queued status updates sync automatically afterwards.
- [ ] B cannot read A's notes through any route (tested). The cloud DB contains no client names or free text (tested).
- [ ] Latest backup restores successfully via `restore_test.sh`.
