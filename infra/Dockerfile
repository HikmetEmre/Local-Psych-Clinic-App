# One image definition for all three services. Build with --build-arg APP=cloud|local|sync.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY pyproject.toml uv.lock alembic.ini ./
COPY packages ./packages
COPY apps ./apps
COPY migrations ./migrations

ARG APP
RUN test -n "$APP" && uv sync --frozen --no-dev --package "klinik-${APP}"

ENV PATH="/srv/.venv/bin:$PATH" \
    APP_MODULE="klinik_${APP}.main:app"

RUN useradd --system --no-create-home klinik
USER klinik

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn \"$APP_MODULE\" --host 0.0.0.0 --port 8000 --proxy-headers"]
