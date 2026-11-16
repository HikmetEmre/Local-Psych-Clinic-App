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
