# Terapiler Tab: Architecture & Implementation Spec

> **Audience:** Claude Code (AI coding agent) working on the Psychology Clinic Management App.
> **Scope:** The `Terapiler` tab. It has an interactive session-log table split by age group (Yaş Grubu), and every session is recorded against a catalog of default therapy types.
> **Status:** Architecture draft. Sections marked `⚠ CONFIRM` need a decision from the developer before implementation.

---

## 1. Context & Constraints

| Item | Value |
|---|---|
| Backend DB | Supabase / PostgreSQL (GCP, Frankfurt region) |
| Security baseline | Row Level Security (RLS) on every table, bcrypt for passwords, Fernet for sensitive free-text |
| App philosophy | Tab-based modules. Each tab owns its tables, API routes, and UI components |
| UI language | Turkish (labels, enums shown to users) |
| Code language | English (table names, columns, variables, enum codes) |

**Rules for the agent:**
- Never hard-code Turkish labels in business logic. Use `code` values in logic and `label_tr` for display.
- Never send clinical notes to the client unencrypted from storage, and never decrypt in the browser. Fernet encrypt/decrypt happens **only in the backend service layer**.
- Every new table gets RLS enabled in the **same migration** that creates it.
- Do not invent new therapy types in code. They come from the seeded catalog (Section 4).

`⚠ CONFIRM`: Frontend framework and backend layer (e.g. FastAPI service vs. Supabase Edge Functions) for Fernet handling.

---

## 2. Core Domain Concepts

### 2.1 Two different things hide inside "therapy type"

The requested list mixes two independent dimensions. Keeping them separate is the most important design decision in this tab.

| Dimension | Answers the question | Examples |
|---|---|---|
| **Yöntem (Modality)** | *What approach/technique is used?* | Oyun Terapisi, EMDR, BDT, Şema Terapi |
| **Format** | *Who is in the room?* | Bireysel, Grup, Aile, Çift |

**Why:** "EMDR" and "Bireysel" are not alternatives to each other. A session can be *EMDR + Bireysel*, and a session can be *Oyun Terapisi + Aile*. With one dropdown you would have to pick one and lose the other, or create combinatorial junk like "Bireysel EMDR". It's like a restaurant order: the *dish* (modality) and *dine-in vs. takeaway* (format) are separate fields.

→ Each session stores **1 format** and **1..n modalities**, with exactly one marked primary.

### 2.2 Age groups (Yaş Grupları)

| Code | Label (TR) | Age range (default) | Sort |
|---|---|---|---|
| `child` | Çocuk | 0–12 | 1 |
| `adolescent` | Ergen | 13–17 | 2 |
| `adult` | Yetişkin | 18–64 | 3 |
| `geriatric` | Geriatri (65+) | 65+ | 4 |

- Ranges live in the DB (`age_groups` table), not in code, so the clinic can change them.
- The age group is **derived** from `clients.birth_date` at the session date.
- The age group is also **snapshotted** on each session row (`age_group_id_at_session`).

**Why snapshot:** a child who turns 13 should not have their past *Çocuk* sessions silently move into the *Ergen* tab. Historical logs must reflect the clinical reality at that time, the same way an invoice keeps the price from the day it was issued.

---

## 3. Data Model

### 3.1 Entity overview

```
age_groups ─┐
            ├── age_group_therapy_defaults ──┐
therapy_methods ─────────────────────────────┘
      │
      └── session_methods ── therapy_sessions ── clients
                                   │
                                   ├── therapists (profiles)
                                   └── audit_log
```

### 3.2 Tables

#### `age_groups`
| Column | Type | Notes |
|---|---|---|
| id | smallserial PK | |
| code | text UNIQUE NOT NULL | `child`, `adolescent`, `adult`, `geriatric` |
| label_tr | text NOT NULL | Display label |
| min_age | smallint NOT NULL | inclusive |
| max_age | smallint NULL | inclusive; NULL = no upper bound |
| sort_order | smallint NOT NULL | Tab order |
| is_active | boolean DEFAULT true | |

Constraint: age ranges must not overlap. Enforce with an exclusion constraint on `int4range(min_age, coalesce(max_age, 200), '[]')`.

#### `therapy_methods` (catalog)
| Column | Type | Notes |
|---|---|---|
| id | serial PK | |
| code | text UNIQUE NOT NULL | e.g. `play_therapy`, `emdr` |
| label_tr | text NOT NULL | e.g. `Oyun Terapisi` |
| kind | text NOT NULL CHECK (kind IN ('modality','format')) | See 2.1 |
| description_tr | text NULL | Tooltip text |
| is_active | boolean DEFAULT true | Soft-disable, never delete (history references it) |
| created_at | timestamptz DEFAULT now() | |

#### `age_group_therapy_defaults`
Maps which methods appear **by default** for each age group.
| Column | Type | Notes |
|---|---|---|
| age_group_id | FK → age_groups | |
| therapy_method_id | FK → therapy_methods | |
| sort_order | smallint | Order in dropdowns/filter chips |
| PK | (age_group_id, therapy_method_id) | |

**Why a mapping table instead of a column on `therapy_methods`:** one method (e.g. EMDR) belongs to several age groups. It's a many-to-many relation.

#### `clients` (existing, referenced)
Required fields for this tab: `id`, `client_code` (pseudonymous, e.g. `DN-2026-0042`), `full_name_encrypted`, `birth_date`, `assigned_therapist_id`, `is_active`.

#### `therapy_sessions` (the patient log)
| Column | Type | Notes |
|---|---|---|
| id | uuid PK DEFAULT gen_random_uuid() | |
| client_id | FK → clients NOT NULL | |
| therapist_id | FK → profiles NOT NULL | |
| age_group_id_at_session | FK → age_groups NOT NULL | Snapshot, see 2.2 |
| format_method_id | FK → therapy_methods NOT NULL | Must have `kind = 'format'` |
| session_number | int NOT NULL | Auto: per-client sequence |
| session_date | timestamptz NOT NULL | |
| duration_min | smallint NOT NULL DEFAULT 50 | CHECK 10–240 |
| status | text NOT NULL | `planned`, `completed`, `cancelled`, `no_show` |
| notes_encrypted | bytea NULL | Fernet token. Clinical notes |
| homework_encrypted | bytea NULL | Fernet token. Optional |
| next_session_date | timestamptz NULL | |
| created_by | FK → profiles | |
| created_at / updated_at | timestamptz | `updated_at` via trigger |
| deleted_at | timestamptz NULL | Soft delete only |

Unique: `(client_id, session_number)`.

#### `session_methods` (modalities used in a session)
| Column | Type | Notes |
|---|---|---|
| session_id | FK → therapy_sessions ON DELETE CASCADE | |
| therapy_method_id | FK → therapy_methods | Must have `kind = 'modality'` |
| is_primary | boolean DEFAULT false | |
| PK | (session_id, therapy_method_id) | |

Partial unique index: one primary per session →
`CREATE UNIQUE INDEX ON session_methods(session_id) WHERE is_primary;`

### 3.3 Integrity rules (enforce in DB, not just UI)

1. `therapy_sessions.format_method_id` must reference a row where `kind = 'format'`. Use a trigger, since CHECK can't query other tables.
2. `session_methods.therapy_method_id` must reference `kind = 'modality'`. Use a trigger.
3. Each `completed` session must have ≥1 modality with exactly one `is_primary = true`. Validate in the backend on save, inside one transaction.
4. `age_group_id_at_session` is set by the backend from `birth_date` + `session_date`. It is **never** accepted from the client payload.
5. `session_number` is computed server-side as `max(session_number)+1` for the client, inside the insert transaction, with row lock on the client.

### 3.4 Indexes
- `therapy_sessions (age_group_id_at_session, session_date DESC)`: tab + default sort
- `therapy_sessions (client_id, session_date DESC)`: patient timeline
- `therapy_sessions (therapist_id, session_date DESC)`: therapist's own view
- `session_methods (therapy_method_id)`: filter by method

---

## 4. Default Therapy Catalog (Seed Data)

### 4.1 Formats (`kind = 'format'`)

| code | label_tr | Available for |
|---|---|---|
| `individual` | Bireysel Terapi | All |
| `group` | Grup Terapisi | All |
| `family` | Aile Terapisi | All |
| `couple` | Çift Terapisi | Yetişkin, Geriatri |
| `parent_session` | Ebeveyn Görüşmesi | Çocuk, Ergen |

### 4.2 Modalities (`kind = 'modality'`)

| code | label_tr | Çocuk | Ergen | Yetişkin | Geriatri |
|---|---|:-:|:-:|:-:|:-:|
| `play_therapy` | Oyun Terapisi | ✅ | | | |
| `sandtray` | Kum Tepsisi Terapisi | ✅ | ✅ | | |
| `art_therapy` | Sanat Terapisi | ✅ | ✅ | ✅ | |
| `emdr` | EMDR | ✅ | ✅ | ✅ | ✅ |
| `cbt` | Bilişsel Davranışçı Terapi (BDT) | ✅ | ✅ | ✅ | ✅ |
| `dbt` | Diyalektik Davranış Terapisi (DBT) | | ✅ | ✅ | |
| `act` | Kabul ve Kararlılık Terapisi (ACT) | | ✅ | ✅ | ✅ |
| `motivational_interviewing` | Motivasyonel Görüşme | | ✅ | ✅ | |
| `schema_therapy` | Şema Terapi | | | ✅ | |
| `psychodynamic` | Psikodinamik Terapi | | | ✅ | ✅ |
| `ipt` | Kişilerarası Terapi (IPT) | | ✅ | ✅ | ✅ |
| `mbct` | Bilinçli Farkındalık Temelli BDT (MBCT) | | | ✅ | ✅ |
| `gestalt` | Gestalt Terapi | | | ✅ | |
| `existential` | Varoluşçu Terapi | | | ✅ | ✅ |
| `reminiscence` | Anı / Yaşam Gözden Geçirme Terapisi | | | | ✅ |
| `supportive` | Destekleyici Terapi | ✅ | ✅ | ✅ | ✅ |

`⚠ CONFIRM`: The clinic's clinical lead should review this matrix. It is a sensible starting point, not a clinical standard, and it's editable in the DB without code changes.

### 4.3 Seed migration (excerpt)

```sql
-- age groups
INSERT INTO age_groups (code, label_tr, min_age, max_age, sort_order) VALUES
  ('child',      'Çocuk',          0, 12,   1),
  ('adolescent', 'Ergen',         13, 17,   2),
  ('adult',      'Yetişkin',      18, 64,   3),
  ('geriatric',  'Geriatri (65+)',65, NULL, 4);

-- formats
INSERT INTO therapy_methods (code, label_tr, kind) VALUES
  ('individual',     'Bireysel Terapi',   'format'),
  ('group',          'Grup Terapisi',     'format'),
  ('family',         'Aile Terapisi',     'format'),
  ('couple',         'Çift Terapisi',     'format'),
  ('parent_session', 'Ebeveyn Görüşmesi', 'format');

-- modalities (subset; complete per table 4.2)
INSERT INTO therapy_methods (code, label_tr, kind) VALUES
  ('play_therapy', 'Oyun Terapisi', 'modality'),
  ('emdr',         'EMDR',          'modality'),
  ('cbt',          'Bilişsel Davranışçı Terapi (BDT)', 'modality');

-- defaults: example for Çocuk
INSERT INTO age_group_therapy_defaults (age_group_id, therapy_method_id, sort_order)
SELECT ag.id, tm.id, x.ord
FROM (VALUES
  ('play_therapy',1),('individual',2),('family',3),('group',4),('emdr',5),
  ('parent_session',6),('cbt',7),('sandtray',8),('art_therapy',9),('supportive',10)
) AS x(code, ord)
JOIN therapy_methods tm ON tm.code = x.code
JOIN age_groups ag ON ag.code = 'child';
```

Seeds must be **idempotent** (`ON CONFLICT (code) DO NOTHING` / `DO UPDATE SET label_tr = EXCLUDED.label_tr`).

---

## 5. Security & Access (RLS)

| Role | therapy_sessions | therapy_methods / age_groups / defaults |
|---|---|---|
| `admin` | Full CRUD (soft delete) on all rows | Full CRUD |
| `therapist` | CRUD only where `therapist_id = auth.uid()` **or** client is assigned to them | SELECT only |
| `secretary` | SELECT/INSERT/UPDATE **scheduling fields only** (`session_date`, `status`, `duration_min`). **No access** to `*_encrypted` columns | SELECT only |

Implementation notes:
- Expose the secretary's view through a **Postgres view** `v_sessions_schedule` that excludes encrypted columns. Grant on the view, not the base table.
- Every read of decrypted notes writes a row to `audit_log` (`actor_id`, `action='notes_read'`, `session_id`, `ts`).
- Every create/update/delete on `therapy_sessions` writes to `audit_log` with a diff that **excludes** encrypted fields.
- Table and list endpoints **never** return decrypted notes. Notes load only in the session detail drawer via a separate endpoint.

**Why the split endpoint:** the list view is where accidental exposure happens (screen sharing, exports, shoulder-surfing). Notes should require a deliberate click, and that click is logged.

---

## 6. UI Specification: `Terapiler` Tab

### 6.1 Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ Terapiler                                        [+ Yeni Seans]      │
├──────────────────────────────────────────────────────────────────────┤
│ [ Tümü 128 ] [ Çocuk 34 ] [ Ergen 21 ] [ Yetişkin 65 ] [ Geriatri 8 ]│  ← Age-group segmented tabs
├──────────────────────────────────────────────────────────────────────┤
│ Yöntem: (Oyun Terapisi)(EMDR)(BDT)(+ Diğer ▾)                        │  ← Chips = defaults of active tab
│ Format: [Tümü ▾]  Terapist: [Tümü ▾]  Durum: [Tümü ▾]                │
│ Tarih: [01.10.2026 – 09.11.2026]   🔍 Danışan kodu / ad              │
├──────────────────────────────────────────────────────────────────────┤
│ ▸ │Danışan    │Yaş│Seans│Tarih      │Yöntem(ler)     │Format  │Durum │
│ ▸ │DN-0042    │ 9 │ #7  │08.11 14:00│Oyun T. +1      │Aile    │✅    │
│ ▾ │DN-0017    │15 │ #3  │07.11 10:00│EMDR            │Bireysel│✅    │
│   │  └─ Patient timeline (last 10 sessions, mini-table)              │
│ ...                                                                  │
├──────────────────────────────────────────────────────────────────────┤
│ 1–25 / 128          ‹ 1 2 3 4 5 ›          Sayfa başına [25 ▾]      │
└──────────────────────────────────────────────────────────────────────┘
```

### 6.2 Age-group tabs
- Rendered from `age_groups` ordered by `sort_order`, plus a leading **Tümü** tab.
- Each tab shows a count badge for the current filters, excluding the age-group filter itself.
- The active tab is stored in the URL query string (`?group=child`). Refresh and share keep state.
- Switching tabs **resets the Yöntem filter** because chip options change, and **keeps** date/therapist/status filters.

### 6.3 Filters
| Filter | Source | Behavior |
|---|---|---|
| Yöntem chips | `age_group_therapy_defaults` for active tab (kind=modality) | Multi-select, OR logic. "Diğer ▾" opens non-default active modalities |
| Format | therapy_methods kind=format | Single select |
| Terapist | profiles with role therapist | Hidden for therapist role (always self) |
| Durum | enum | Multi-select |
| Tarih aralığı | date range | Default: last 30 days |
| Arama | client_code, decrypted name match | Debounced 300 ms. Name search runs server-side |

All filtering, sorting and pagination happen **server-side**. The client never downloads the full log.

### 6.4 Table columns
| Column | Sortable | Notes |
|---|:-:|---|
| Expand ▸ | | Opens patient timeline |
| Danışan | ✅ | `client_code` by default. Full name only on hover/detail for authorized roles |
| Yaş | ✅ | Age at session date |
| Seans | ✅ | `#session_number` |
| Tarih | ✅ (default DESC) | `dd.MM.yyyy HH:mm`, Europe/Istanbul |
| Yöntem(ler) | | Primary label + `+N` badge, tooltip lists all |
| Format | ✅ | |
| Süre | ✅ | minutes |
| Terapist | ✅ | Hidden for therapist role |
| Durum | ✅ | Colored badge: Planlandı / Tamamlandı / İptal / Gelmedi |
| Aksiyonlar | | Görüntüle · Düzenle · Sil (role-based) |

Column visibility toggle is persisted per user in local preferences.

### 6.5 Row expansion: patient timeline
- Shows the selected client's last 10 sessions across **all** age groups (a client may have Çocuk and Ergen history).
- Each item: date, session #, primary modality, format, status.
- Footer link: "Tüm geçmişi aç" opens the client's profile tab.

### 6.6 "Yeni Seans" / Edit modal

Flow:
1. **Danışan seç**: searchable select by code/name.
2. System computes age at selected `session_date` and shows a read-only badge, e.g. `Yaş Grubu: Çocuk (9)`.
3. **Format**: dropdown limited to formats mapped to that age group. Default `Bireysel Terapi`.
4. **Yöntem(ler)**: multi-select.
   - Group 1: *Varsayılanlar*, the age group's defaults in `sort_order`
   - Group 2: *Diğer yöntemler*, the remaining active modalities
   - Exactly one must be starred as primary (★). The first selected is auto-primary.
5. Tarih/Saat, Süre (default 50), Durum (default `planned` if future, `completed` if past).
6. **Seans Notu** and **Ödev**: textarea, sent to backend, Fernet-encrypted before insert.
7. Sonraki seans tarihi (optional). If set, offer "Planlanan seans olarak oluştur" checkbox.

Edge cases:
- If `session_date` changes and that moves the client into a different age group, re-compute the badge and **warn** if selected methods are not in the new group's defaults. Warn only, don't block.
- Selecting a client whose `birth_date` is missing blocks save with message: *"Danışanın doğum tarihi eksik. Önce danışan profilini güncelleyin."*

### 6.7 States
- **Loading:** skeleton rows (not a spinner) to avoid layout shift.
- **Empty (no data):** "Bu yaş grubunda henüz seans kaydı yok." + [Yeni Seans] button.
- **Empty (filters):** "Filtrelere uyan kayıt bulunamadı." + [Filtreleri temizle].
- **Error:** inline banner with retry. Never show raw DB errors.

### 6.8 Export
- CSV export of the **current filtered view**: admin only.
- Export **never** includes notes/homework.
- Each export writes an `audit_log` entry with the filter set used.

---

## 7. API Contract

All routes require auth. Responses use `code` values plus `label_tr` for display.

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/therapy/age-groups` | Tabs + ranges |
| GET | `/api/therapy/methods?age_group=child` | Defaults + other methods, grouped |
| GET | `/api/therapy/sessions` | Paginated list (query params below) |
| GET | `/api/therapy/sessions/counts` | Badge counts per age group for current filters |
| GET | `/api/therapy/sessions/{id}` | Detail **with** decrypted notes (logged) |
| POST | `/api/therapy/sessions` | Create |
| PATCH | `/api/therapy/sessions/{id}` | Update |
| DELETE | `/api/therapy/sessions/{id}` | Soft delete (admin) |
| GET | `/api/clients/{id}/sessions?limit=10` | Timeline |

`GET /sessions` query params:
`age_group`, `method[]`, `format`, `therapist_id`, `status[]`, `date_from`, `date_to`, `q`, `sort` (e.g. `-session_date`), `page`, `page_size` (max 100).

Example list item:
```json
{
  "id": "6f1c…",
  "client": { "id": "…", "code": "DN-2026-0042", "age_at_session": 9 },
  "age_group": { "code": "child", "label_tr": "Çocuk" },
  "session_number": 7,
  "session_date": "2026-11-08T14:00:00+03:00",
  "duration_min": 50,
  "format": { "code": "family", "label_tr": "Aile Terapisi" },
  "methods": [
    { "code": "play_therapy", "label_tr": "Oyun Terapisi", "is_primary": true },
    { "code": "cbt", "label_tr": "Bilişsel Davranışçı Terapi (BDT)", "is_primary": false }
  ],
  "status": "completed",
  "therapist": { "id": "…", "display_name": "Uzm. Psk. A. Y." }
}
```

`POST /sessions` body:
```json
{
  "client_id": "…",
  "session_date": "2026-11-10T15:00:00+03:00",
  "duration_min": 50,
  "format_code": "individual",
  "methods": [{ "code": "emdr", "is_primary": true }],
  "status": "planned",
  "notes": "plain text, encrypted server-side",
  "homework": null,
  "next_session_date": null
}
```
The server rejects any payload containing `age_group_id_at_session`, `session_number`, or `therapist_id` (for the therapist role).

---

## 8. Implementation Order (Task Breakdown)

Work in this order. Each step should be a separate commit with passing tests.

1. **Migration 1: catalog:** `age_groups`, `therapy_methods`, `age_group_therapy_defaults` + RLS + kind-check triggers.
2. **Migration 2: seed:** idempotent seed from Section 4.
3. **Migration 3: sessions:** `therapy_sessions`, `session_methods`, indexes, `updated_at` trigger, RLS, `v_sessions_schedule` view.
4. **Backend service:** age-group resolver `resolve_age_group(birth_date, at_date)`, session-number allocator, Fernet helpers, audit logger.
5. **API routes** (Section 7) with validation for the rules in 3.3.
6. **UI: read path:** tabs, filters, table, pagination, counts, URL state.
7. **UI: write path:** Yeni Seans/Edit modal with default-method grouping.
8. **UI: row expansion timeline + detail drawer** (decrypted notes, logged).
9. **Export** (admin) + audit.
10. **Tests** (Section 9).

---

## 9. Acceptance Criteria & Tests

### Functional
- [ ] Tabs render from DB. Adding a new age group in DB shows a new tab with no code change.
- [ ] Çocuk tab's Yöntem chips show exactly the seeded Çocuk defaults in `sort_order`.
- [ ] A session for a 12-year-old saved today stays under **Çocuk** after the client turns 13.
- [ ] Creating a session with format `EMDR` (a modality) is rejected by the DB trigger.
- [ ] Creating a completed session with zero modalities, or two primaries, is rejected.
- [ ] Filters, sort and page survive browser refresh via URL.

### Security
- [ ] Therapist A cannot list, read or update Therapist B's sessions (RLS test with two JWTs).
- [ ] Secretary role receives no `notes_encrypted`/`homework_encrypted` fields from any endpoint.
- [ ] `GET /sessions` list response never contains decrypted note text.
- [ ] Opening the detail drawer creates an `audit_log` row.
- [ ] Notes in DB are unreadable without the Fernet key (assert stored bytes ≠ plaintext).

### Unit tests (backend)
- `resolve_age_group`: boundaries 12/13, 17/18, 64/65, and birthday-on-session-date.
- Session-number allocator under concurrent inserts (two transactions, same client → 7 and 8, no duplicate).
- Payload sanitizer strips/rejects server-owned fields.

---

## 10. Out of Scope (for now)

- Billing/invoicing per session (belongs to a future `Muhasebe` tab).
- Calendar/scheduling grid UI (future `Randevular` tab. It reuses `therapy_sessions` with `status = planned`).
- Clinical outcome scales (e.g. questionnaires). These would need their own tables.
- Bulk import of historical sessions.

---

## 11. Glossary (TR ↔ code)

| Türkçe | Code / table |
|---|---|
| Danışan | `clients` |
| Seans kaydı | `therapy_sessions` |
| Yaş grubu | `age_groups` |
| Terapi yöntemi | `therapy_methods` (kind=`modality`) |
| Seans formatı | `therapy_methods` (kind=`format`) |
| Varsayılan yöntemler | `age_group_therapy_defaults` |
| Planlandı / Tamamlandı / İptal / Gelmedi | `planned` / `completed` / `cancelled` / `no_show` |
