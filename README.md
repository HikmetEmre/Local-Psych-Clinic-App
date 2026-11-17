# Nil Psikoloji: Klinik App

Management app for a small psychology clinic. **One program on the office computer** with two
roles, and an **offline-capable copy on the owner's laptop** that syncs with the office.

| Role | Sees and does |
|---|---|
| **Yönetici** (owner) | Everything: every therapist's sessions and notes, all income, reports, users and settings |
| **Terapist** | The whole calendar (and booking), their own clients' sessions and notes, their own income |

Tabs: **Ana sayfa** (the day's appointments and sessions, month calendar with quick booking),
**Takvim**, **Terapiler**, **Danışanlar**, **Gelir**, **Raporlar** (detailed PDF reports),
**Günlük özet** (encrypted daily snapshot in Google Drive), **Ayarlar**.

Code: `apps/local` (the app, `klinik_local`), `packages/shared` (auth, UI, calendar),
`migrations/local`, `installer/` (Windows installer), `scripts/` (build).
Specs: [`docs/`](docs/) (`PLAN_MODE_BRIEF.md`, `TERAPILER_TAB.md`, `IMPLEMENTATION_PLAN.md`).

> History: until 2026-11-18 the schedule and payments lived in a cloud app (Klinik Web) synced
> with the office. That was replaced by this single office app; see git history.

## Running it for real

| Topic | Runbook |
|---|---|
| Installing on the office computer and the owner's laptop | [docs/runbooks/office-installer.md](docs/runbooks/office-installer.md) |
| Windows settings for the office computer | [docs/runbooks/office-laptop-windows.md](docs/runbooks/office-laptop-windows.md) §1 |
| Backups, key custody, monthly restore test | [docs/runbooks/backups.md](docs/runbooks/backups.md) |
| Go-live checklist | [docs/runbooks/go-live.md](docs/runbooks/go-live.md) |

## Development

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker Desktop (running).

```sh
uv sync                      # install everything into .venv
uv run pytest                # unit + integration tests (integration needs Docker)
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Run it locally (Docker):

```sh
docker compose --env-file infra/dev.env -f infra/docker-compose.yml up -d --build --wait
```

The app: http://127.0.0.1:8002

Create a user (the installer does this for the admin; afterwards the admin adds users in
**Ayarlar → Kullanıcılar** with a role):

```sh
docker compose --env-file infra/dev.env -f infra/docker-compose.yml run --rm -T local-migrate \
  python -m klinik_local.manage create-user --username nil.yilmaz --name "Nil Yılmaz" --role admin
```

Each new user gets a one-time password, must change it at first login, and sets up two-step
verification (TOTP) with an authenticator app.

Build the Windows installer: `python scripts/build_installer.py` (needs Inno Setup 6).

## Migrations

```sh
uv run alembic -n local upgrade head   # needs LOCAL_MIGRATION_DATABASE_URL
```

## Rules that must not be broken

- Client identity and clinical text are Fernet-encrypted in the service layer; the keys never
  leave the office computer except inside the passphrase-protected laptop pairing file.
- Row-level security in PostgreSQL enforces "therapist sees own" as a second layer.
- The laptop copy never overwrites the office silently: conflicts wait for the owner.
- Real `.env` files and keys are never committed. See `.env.example`.
