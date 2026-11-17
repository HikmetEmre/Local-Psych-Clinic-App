"""Web Push delivery (brief §8.2). Runs as a background loop in the cloud app.

- picks due rows (``push_after`` / ``next_attempt_at`` passed) with SKIP LOCKED, so
  several app instances never send the same notification twice;
- a subscription answering 404/410 is gone for good and is deleted;
- other failures retry after 30 s, 2 min, 10 min, 1 h; after 5 attempts: failed;
- already read, expired, push turned off, or no device: skipped.
The inbox row stays in every case.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from klinik_cloud import tables as t
from klinik_cloud.bildirimler.service import render, settings_for
from klinik_shared.timeutil import now_utc

log = logging.getLogger("klinik_cloud.push")

MAX_ATTEMPTS = 5
BACKOFF = (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10), timedelta(hours=1))
GONE = (404, 410)


@dataclass(frozen=True)
class Subscription:
    endpoint: str
    p256dh: str
    auth: str


class PushSender(Protocol):
    async def send(self, subscription: Subscription, data: dict[str, Any]) -> int:
        """Deliver one message; return the push service's HTTP status (0 = no answer)."""
        ...


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_vapid_keys() -> tuple[str, str]:
    """(private key: base64url DER, public key: base64url uncompressed point)."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64url(private), _b64url(public)


class WebPushSender:
    """Real delivery through the browser vendor's push service (FCM for Android Chrome)."""

    def __init__(self, private_key: str, subject: str) -> None:
        self.private_key = private_key
        self.subject = subject

    def _send_sync(self, subscription: Subscription, data: dict[str, Any]) -> int:
        from pywebpush import WebPushException, webpush

        try:
            response = webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=json.dumps(data, ensure_ascii=False),
                vapid_private_key=self.private_key,
                vapid_claims={"sub": self.subject},
                ttl=24 * 3600,
                timeout=10,
            )
        except WebPushException as exc:
            return int(exc.response.status_code) if exc.response is not None else 0
        except Exception:
            return 0
        return int(getattr(response, "status_code", 201))

    async def send(self, subscription: Subscription, data: dict[str, Any]) -> int:
        return await asyncio.to_thread(self._send_sync, subscription, data)


@dataclass
class DeliveryReport:
    sent: int = 0
    skipped: int = 0
    retried: int = 0
    failed: int = 0
    removed_subscriptions: int = 0
    details: list[str] = field(default_factory=list)


async def deliver_due(
    engine: AsyncEngine, sender: PushSender, *, now: datetime | None = None, limit: int = 50
) -> DeliveryReport:
    now = now or now_utc()
    report = DeliveryReport()
    n, s = t.notifications, t.push_subscriptions
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(n)
                .where(
                    n.c.push_state == "pending",
                    text("coalesce(next_attempt_at, push_after) <= :now").bindparams(now=now),
                )
                .order_by(n.c.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            settings = await settings_for(conn, row.recipient_id)
            if (
                row.read_at is not None
                or (row.expires_at is not None and row.expires_at <= now)
                or not settings.push_enabled
            ):
                await conn.execute(update(n).where(n.c.id == row.id).values(push_state="skipped"))
                report.skipped += 1
                continue
            subs = (await conn.execute(select(s).where(s.c.user_id == row.recipient_id))).all()
            if not subs:
                await conn.execute(update(n).where(n.c.id == row.id).values(push_state="skipped"))
                report.skipped += 1
                continue
            message = render(row.event_type, row.payload)
            data = {
                "title": message.title,
                "body": message.body,
                "url": message.url,
                "tag": message.tag,
            }
            delivered = False
            transient = False
            for sub in subs:
                status = await sender.send(Subscription(sub.endpoint, sub.p256dh, sub.auth), data)
                if 200 <= status < 300:
                    delivered = True
                    await conn.execute(
                        update(s)
                        .where(s.c.id == sub.id)
                        .values(last_success_at=now, failure_count=0)
                    )
                elif status in GONE:
                    await conn.execute(delete(s).where(s.c.id == sub.id))
                    report.removed_subscriptions += 1
                else:
                    transient = True
                    await conn.execute(
                        update(s)
                        .where(s.c.id == sub.id)
                        .values(failure_count=s.c.failure_count + 1)
                    )
            if delivered:
                await conn.execute(
                    update(n)
                    .where(n.c.id == row.id)
                    .values(push_state="sent", pushed_at=now, push_attempts=n.c.push_attempts + 1)
                )
                report.sent += 1
            elif transient:
                attempts = row.push_attempts + 1
                if attempts >= MAX_ATTEMPTS:
                    await conn.execute(
                        update(n)
                        .where(n.c.id == row.id)
                        .values(push_state="failed", push_attempts=attempts)
                    )
                    report.failed += 1
                else:
                    await conn.execute(
                        update(n)
                        .where(n.c.id == row.id)
                        .values(
                            push_attempts=attempts,
                            next_attempt_at=now + BACKOFF[min(attempts, len(BACKOFF)) - 1],
                        )
                    )
                    report.retried += 1
            else:  # every subscription was gone
                await conn.execute(update(n).where(n.c.id == row.id).values(push_state="skipped"))
                report.skipped += 1
    return report


async def push_loop(engine: AsyncEngine, sender: PushSender, interval: float = 5.0) -> None:
    while True:
        try:
            await deliver_due(engine, sender)
        except Exception:
            log.exception("push delivery failed")
        await asyncio.sleep(interval)


async def reminder_loop(engine: AsyncEngine, interval: float = 60.0) -> None:
    from klinik_cloud.bildirimler.service import schedule_reminders

    while True:
        try:
            async with engine.begin() as conn:
                await schedule_reminders(conn)
        except Exception:
            log.exception("reminder scheduling failed")
        await asyncio.sleep(interval)
