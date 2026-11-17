"""Sync worker process. Run with: uvicorn klinik_sync.main:create_app --factory

Serves only /healthz (for Docker); the work happens in background tasks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from klinik_shared.db import db_status, make_engine
from klinik_sync.cloud_client import CloudClient
from klinik_sync.config import Settings, get_settings
from klinik_sync.store import LocalStore
from klinik_sync.worker import SyncWorker

log = logging.getLogger("klinik_sync")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.local_database_url)
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if settings.can_run and engine is not None:
            cloud = CloudClient(settings.cloud_base_url or "", settings.cloud_token or "")
            worker = SyncWorker(
                LocalStore(engine),
                cloud,
                pull_wait=settings.pull_wait_seconds,
                push_interval=settings.push_interval_seconds,
            )
            state["worker"] = worker
            task = asyncio.create_task(worker.run(), name="sync-worker")
        else:
            log.warning("sync worker not started: database, cloud URL or token missing")
        try:
            yield
        finally:
            if task is not None:
                state["worker"].stop()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await state["worker"].cloud.aclose()
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title="klinik-sync", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)  # fmt: skip

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        db = await db_status(engine)
        body: dict[str, Any] = {"status": "ok", "service": "klinik-sync", "db": db}
        if db == "ok" and engine is not None:
            try:
                sync = await LocalStore(engine).get_state()
            except Exception:
                sync = {}
            body["sync"] = {
                key: sync.get(key)
                for key in ("worker_state", "last_pull_ok", "last_push_ok", "last_error")
            }
            body["sync"]["configured"] = settings.can_run
        healthy = db != "down"
        if not healthy:
            body["status"] = "degraded"
        return JSONResponse(body, status_code=200 if healthy else 503)

    return app
