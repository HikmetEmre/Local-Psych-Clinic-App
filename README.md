# Nil Psikoloji: Klinik App

Management app for a two-therapist psychology clinic, split into two tiers:

| Tier | App | Holds | Runs on |
|---|---|---|---|
| Cloud | **Klinik Web** (`apps/cloud`) | Schedule, payments, notifications. Clients appear only as codes (`DN-2026-0042`) | Cloud Run + Supabase Postgres |
| Local | **Klinik Kasa** (`apps/local`) | Client identity, encrypted session notes and logs | Office laptop, Docker, Tailscale only |
| — | **Sync worker** (`apps/sync`) | Mirrors appointments to the office, pushes codes and outcomes | Office laptop, outbound only |

Specs and the plan are in [`docs/`](docs/): `PLAN_MODE_BRIEF.md`, `TERAPILER_TAB.md`, `IMPLEMENTATION_PLAN.md`.

## Development

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker Desktop (running).

```sh
uv sync                      # install everything into .venv
uv run pytest                # unit + integration tests (integration needs Docker)
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Run both tiers locally:

```sh
docker compose --env-file infra/dev.env -f infra/docker-compose.yml -f infra/docker-compose.dev.yml up -d --build --wait
python scripts/smoke_healthz.py
```

- Klinik Web: http://127.0.0.1:8001
- Klinik Kasa: http://127.0.0.1:8002

## Kullanıcı ekleme (adding users)

Users are created from one TOML file so that their ids are identical in both tiers
(see `infra/users.example.toml`). Each new user gets a one-time password, must change it at
first login, and sets up two-step verification (TOTP) with an authenticator app.

```sh
# development stack
docker compose --env-file infra/dev.env -f infra/docker-compose.yml -f infra/docker-compose.dev.yml   run --rm -T local-migrate python -m klinik_local.manage bootstrap-users - < infra/users.example.toml
```

Permissions, deactivation, password reset and 2FA reset are then managed in **Ayarlar → Kullanıcılar**.

The cloud tier (Klinik Web) uses the same file, so the two therapists have the same ids in both:

```sh
docker compose --env-file infra/dev.env -f infra/docker-compose.yml -f infra/docker-compose.dev.yml   run --rm -T cloud-migrate python -m klinik_cloud.manage bootstrap-users - < infra/users.example.toml
```

New keys for a real installation: `python -m klinik_local.manage generate-keys` (office) and
`python -m klinik_cloud.manage generate-keys` (cloud).

Rooms are added in Klinik Web under **Ayarlar → Odalar**. Client codes reach the cloud from the
office through the sync worker (milestone 5).

## Browser tests

`tests/e2e` drives real browsers (Playwright) against the running dev stack. They are skipped
unless you pass a JSON file with the dev users' passwords and TOTP secrets:

```sh
uv run playwright install chromium
uv run pytest tests/e2e --e2e-users=path/to/users.json
```

## Migrations

Two independent Alembic chains:

```sh
uv run alembic -n cloud upgrade head   # needs CLOUD_MIGRATION_DATABASE_URL
uv run alembic -n local upgrade head   # needs LOCAL_MIGRATION_DATABASE_URL
```

## Rules that must not be broken

- Every table has exactly one owner tier, and only that tier writes it (brief §3).
- The cloud never stores client names, contact details, birth dates, free text or therapy methods.
- Clinical text is Fernet-encrypted in the backend service layer and never decrypted in the browser.
- Real `.env` files and keys are never committed. See `.env.example`.
