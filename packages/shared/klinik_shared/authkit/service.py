"""Auth data operations. Every method runs inside the caller's transaction."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from cryptography.fernet import InvalidToken, MultiFernet
from sqlalchemy import Table, and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_shared.audit import write_audit
from klinik_shared.authkit import passwords, totp
from klinik_shared.authkit.tables import AuthTables
from klinik_shared.permissions import ALL_PERMISSION_VALUES, Permission
from klinik_shared.timeutil import now_utc

TOUCH_INTERVAL = timedelta(seconds=60)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii", "ignore")).digest()


def username_key(username: str) -> str:
    return hashlib.sha256(normalize_username(username).encode("utf-8")).hexdigest()


def normalize_username(username: str) -> str:
    return username.strip().lower()


class LoginStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    LOCKED = "locked"


@dataclass(frozen=True)
class PasswordLoginResult:
    status: LoginStatus
    token: str | None = None
    needs_enrollment: bool = False


@dataclass(frozen=True)
class PendingSession:
    """Password accepted, second factor not yet passed."""

    id_hash: bytes
    user_id: UUID
    username: str
    totp_enrolled: bool


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    username: str
    display_name: str
    permissions: frozenset[str]
    must_change_password: bool
    session_id_hash: bytes
    csrf_token: str

    def has(self, permission: Permission | str) -> bool:
        return str(permission) in self.permissions


@dataclass(frozen=True)
class AuthPolicy:
    idle_timeout: timedelta = timedelta(minutes=30)
    pending_timeout: timedelta = timedelta(minutes=10)
    max_lifetime: timedelta = timedelta(hours=12)
    max_failures: int = 5
    lock_window: timedelta = timedelta(minutes=15)
    # Many usernames tried from one address: block the address, not only the account.
    max_failures_per_ip: int = 20


class AuthStore:
    def __init__(
        self,
        tables: AuthTables,
        audit_table: Table,
        cipher: MultiFernet,
        policy: AuthPolicy | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.t = tables
        self.audit_table = audit_table
        self.cipher = cipher
        self.policy = policy or AuthPolicy()
        self.clock = clock

    # ------------------------------------------------------------ lockout

    async def is_locked(self, conn: AsyncConnection, username: str) -> bool:
        la = self.t.login_attempts
        key = username_key(username)
        last_success = (
            await conn.execute(
                select(func.max(la.c.ts)).where(la.c.username_key == key, la.c.success)
            )
        ).scalar_one_or_none()
        since = self.clock() - self.policy.lock_window
        if last_success is not None and last_success > since:
            since = last_success
        failures = (
            await conn.execute(
                select(func.count())
                .select_from(la)
                .where(la.c.username_key == key, la.c.success.is_(False), la.c.ts > since)
            )
        ).scalar_one()
        return bool(failures >= self.policy.max_failures)

    async def _record_attempt(
        self, conn: AsyncConnection, username: str, ip: str | None, success: bool
    ) -> None:
        await conn.execute(
            insert(self.t.login_attempts).values(
                username_key=username_key(username), ip=ip, ts=self.clock(), success=success
            )
        )

    # ------------------------------------------------------------ step 1: password

    async def is_ip_blocked(self, conn: AsyncConnection, ip: str | None) -> bool:
        if not ip:
            return False
        la = self.t.login_attempts
        failures = (
            await conn.execute(
                select(func.count())
                .select_from(la)
                .where(
                    la.c.ip == ip,
                    la.c.success.is_(False),
                    la.c.ts > self.clock() - self.policy.lock_window,
                )
            )
        ).scalar_one()
        return bool(failures >= self.policy.max_failures_per_ip)

    async def password_login(
        self, conn: AsyncConnection, username: str, password: str, ip: str | None
    ) -> PasswordLoginResult:
        if await self.is_locked(conn, username) or await self.is_ip_blocked(conn, ip):
            await write_audit(
                conn,
                self.audit_table,
                action="login_locked",
                actor_id=None,
                actor_kind="system",
                ip=ip,
            )
            return PasswordLoginResult(LoginStatus.LOCKED)

        users = self.t.users
        row = (
            await conn.execute(
                select(users).where(users.c.username == normalize_username(username))
            )
        ).one_or_none()
        if row is None or not row.is_active:
            await asyncio.to_thread(passwords.dummy_verify, password)
            valid = False
        else:
            valid = await asyncio.to_thread(passwords.verify_password, password, row.password_hash)

        await self._record_attempt(conn, username, ip, valid)
        if not valid or row is None:
            await write_audit(
                conn,
                self.audit_table,
                action="login_failed",
                actor_id=row.id if row is not None else None,
                actor_kind="user" if row is not None else "system",
                detail={"stage": "password"},
                ip=ip,
            )
            return PasswordLoginResult(LoginStatus.INVALID)

        token = await self._create_session(conn, row.id, ip, passed=False)
        return PasswordLoginResult(
            LoginStatus.OK, token=token, needs_enrollment=row.totp_confirmed_at is None
        )

    async def _create_session(
        self, conn: AsyncConnection, user_id: UUID, ip: str | None, *, passed: bool
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        await conn.execute(
            insert(self.t.auth_sessions).values(
                id_hash=hash_token(token),
                user_id=user_id,
                csrf_token=secrets.token_urlsafe(32),
                created_at=now,
                last_seen_at=now,
                totp_passed_at=now if passed else None,
                ip=ip,
            )
        )
        return token

    # ------------------------------------------------------------ step 2: second factor

    async def get_pending(self, conn: AsyncConnection, token: str | None) -> PendingSession | None:
        if not token:
            return None
        s, u = self.t.auth_sessions, self.t.users
        row = (
            await conn.execute(
                select(s.c.id_hash, u.c.id, u.c.username, u.c.totp_confirmed_at)
                .select_from(s.join(u, u.c.id == s.c.user_id))
                .where(
                    s.c.id_hash == hash_token(token),
                    s.c.totp_passed_at.is_(None),
                    s.c.revoked_at.is_(None),
                    s.c.created_at > self.clock() - self.policy.pending_timeout,
                    u.c.is_active,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return PendingSession(row.id_hash, row.id, row.username, row.totp_confirmed_at is not None)

    async def verify_second_factor(
        self, conn: AsyncConnection, pending: PendingSession, code: str, ip: str | None
    ) -> str | None:
        """Returns a fresh, fully authenticated session token, or None."""
        if await self.is_locked(conn, pending.username):
            return None
        users = self.t.users
        user = (
            await conn.execute(select(users).where(users.c.id == pending.user_id).with_for_update())
        ).one()

        ok = False
        if user.totp_confirmed_at is not None and user.totp_secret_enc is not None:
            secret = self._decrypt(user.totp_secret_enc)
            step = totp.matching_step(secret, code, self.clock()) if secret else None
            if step is not None and (user.totp_last_step is None or step > user.totp_last_step):
                await conn.execute(
                    update(users).where(users.c.id == user.id).values(totp_last_step=step)
                )
                ok = True
        if not ok:
            ok = await self._use_recovery_code(conn, pending.user_id, code)

        await self._record_attempt(conn, pending.username, ip, ok)
        if not ok:
            await write_audit(
                conn,
                self.audit_table,
                action="login_failed",
                actor_id=pending.user_id,
                detail={"stage": "second_factor"},
                ip=ip,
            )
            return None
        return await self._promote(conn, pending, ip)

    async def _use_recovery_code(self, conn: AsyncConnection, user_id: UUID, code: str) -> bool:
        rc = self.t.recovery_codes
        used = (
            await conn.execute(
                update(rc)
                .where(
                    rc.c.user_id == user_id,
                    rc.c.code_hash == totp.hash_recovery_code(code),
                    rc.c.used_at.is_(None),
                )
                .values(used_at=self.clock())
                .returning(rc.c.id)
            )
        ).first()
        if used is not None:
            await write_audit(conn, self.audit_table, action="recovery_code_used", actor_id=user_id)
        return used is not None

    async def _promote(self, conn: AsyncConnection, pending: PendingSession, ip: str | None) -> str:
        """Replace the pending session with a new token (prevents session fixation)."""
        s = self.t.auth_sessions
        await conn.execute(delete(s).where(s.c.id_hash == pending.id_hash))
        token = await self._create_session(conn, pending.user_id, ip, passed=True)
        await write_audit(
            conn, self.audit_table, action="login_ok", actor_id=pending.user_id, ip=ip
        )
        return token

    # ------------------------------------------------------------ enrollment

    async def start_enrollment(self, conn: AsyncConnection, pending: PendingSession) -> str:
        """The TOTP secret to show the user (created once, reused until confirmed)."""
        users = self.t.users
        user = (
            await conn.execute(select(users).where(users.c.id == pending.user_id).with_for_update())
        ).one()
        if user.totp_confirmed_at is not None:
            raise PermissionError("second factor is already enrolled")
        if user.totp_secret_enc is not None:
            existing = self._decrypt(user.totp_secret_enc)
            if existing:
                return existing
        secret = totp.new_secret()
        await conn.execute(
            update(users)
            .where(users.c.id == user.id)
            .values(totp_secret_enc=self.cipher.encrypt(secret.encode("ascii")))
        )
        return secret

    async def confirm_enrollment(
        self, conn: AsyncConnection, pending: PendingSession, code: str, ip: str | None
    ) -> tuple[str, list[str]] | None:
        """On success: (new session token, recovery codes to show exactly once)."""
        users = self.t.users
        user = (
            await conn.execute(select(users).where(users.c.id == pending.user_id).with_for_update())
        ).one()
        if user.totp_confirmed_at is not None or user.totp_secret_enc is None:
            return None
        secret = self._decrypt(user.totp_secret_enc)
        step = totp.matching_step(secret, code, self.clock()) if secret else None
        if step is None:
            return None
        await conn.execute(
            update(users)
            .where(users.c.id == user.id)
            .values(totp_confirmed_at=self.clock(), totp_last_step=step)
        )
        codes = totp.generate_recovery_codes()
        rc = self.t.recovery_codes
        await conn.execute(delete(rc).where(rc.c.user_id == user.id))
        await conn.execute(
            insert(rc),
            [{"user_id": user.id, "code_hash": totp.hash_recovery_code(c)} for c in codes],
        )
        await write_audit(conn, self.audit_table, action="totp_enrolled", actor_id=user.id, ip=ip)
        return await self._promote(conn, pending, ip), codes

    def _decrypt(self, token: bytes) -> str | None:
        try:
            return self.cipher.decrypt(bytes(token)).decode("ascii")
        except InvalidToken:
            return None

    # ------------------------------------------------------------ sessions

    async def resolve(
        self, conn: AsyncConnection, token: str | None, *, touch: bool = True
    ) -> CurrentUser | None:
        """The logged-in user for a session token, enforcing idle and absolute timeouts."""
        if not token:
            return None
        s, u = self.t.auth_sessions, self.t.users
        row = (
            await conn.execute(
                select(
                    s.c.id_hash,
                    s.c.csrf_token,
                    s.c.created_at,
                    s.c.last_seen_at,
                    u.c.id,
                    u.c.username,
                    u.c.display_name,
                    u.c.must_change_password,
                )
                .select_from(s.join(u, u.c.id == s.c.user_id))
                .where(
                    s.c.id_hash == hash_token(token),
                    s.c.totp_passed_at.is_not(None),
                    s.c.revoked_at.is_(None),
                    u.c.is_active,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        now = self.clock()
        expired = (
            now - row.last_seen_at > self.policy.idle_timeout
            or now - row.created_at > self.policy.max_lifetime
        )
        if expired:
            await conn.execute(update(s).where(s.c.id_hash == row.id_hash).values(revoked_at=now))
            return None
        if touch and now - row.last_seen_at > TOUCH_INTERVAL:
            await conn.execute(update(s).where(s.c.id_hash == row.id_hash).values(last_seen_at=now))
        up = self.t.user_permissions
        permissions = (
            await conn.execute(select(up.c.permission).where(up.c.user_id == row.id))
        ).scalars()
        return CurrentUser(
            id=row.id,
            username=row.username,
            display_name=row.display_name,
            permissions=frozenset(permissions),
            must_change_password=row.must_change_password,
            session_id_hash=row.id_hash,
            csrf_token=row.csrf_token,
        )

    async def logout(self, conn: AsyncConnection, token: str | None) -> None:
        if not token:
            return
        s = self.t.auth_sessions
        row = (
            await conn.execute(
                update(s)
                .where(s.c.id_hash == hash_token(token), s.c.revoked_at.is_(None))
                .values(revoked_at=self.clock())
                .returning(s.c.user_id)
            )
        ).first()
        if row is not None:
            await write_audit(conn, self.audit_table, action="logout", actor_id=row.user_id)

    async def revoke_sessions(
        self, conn: AsyncConnection, user_id: UUID, *, except_id_hash: bytes | None = None
    ) -> None:
        s = self.t.auth_sessions
        condition = and_(s.c.user_id == user_id, s.c.revoked_at.is_(None))
        if except_id_hash is not None:
            condition = and_(condition, s.c.id_hash != except_id_hash)
        await conn.execute(update(s).where(condition).values(revoked_at=self.clock()))

    # ------------------------------------------------------------ account

    async def change_password(
        self, conn: AsyncConnection, user: CurrentUser, current: str, new: str
    ) -> bool:
        """False if ``current`` is wrong. Raises PasswordPolicyError for a weak new one."""
        users = self.t.users
        row = (
            await conn.execute(select(users).where(users.c.id == user.id).with_for_update())
        ).one()
        if not await asyncio.to_thread(passwords.verify_password, current, row.password_hash):
            return False
        passwords.validate_new_password(new)
        if new == current:
            raise passwords.PasswordPolicyError("Yeni şifre eskisiyle aynı olamaz.")
        new_hash = await asyncio.to_thread(passwords.hash_password, new)
        await conn.execute(
            update(users)
            .where(users.c.id == user.id)
            .values(password_hash=new_hash, must_change_password=False, updated_at=self.clock())
        )
        await self.revoke_sessions(conn, user.id, except_id_hash=user.session_id_hash)
        await write_audit(conn, self.audit_table, action="password_changed", actor_id=user.id)
        return True

    # ------------------------------------------------------------ administration

    async def ensure_user(
        self,
        conn: AsyncConnection,
        *,
        user_id: UUID,
        username: str,
        display_name: str,
        permissions: Iterable[Permission | str],
    ) -> str | None:
        """Create the user if missing. Returns the temporary password for a new user.

        An existing user is left untouched (permissions edited in the admin page win).
        """
        users = self.t.users
        exists = (await conn.execute(select(users.c.id).where(users.c.id == user_id))).first()
        if exists is not None:
            return None
        temporary = passwords.generate_temporary_password()
        now = self.clock()
        await conn.execute(
            insert(users).values(
                id=user_id,
                username=normalize_username(username),
                display_name=display_name,
                password_hash=await asyncio.to_thread(passwords.hash_password, temporary),
                must_change_password=True,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )
        await self.set_permissions(conn, user_id, permissions, actor_id=None)
        await write_audit(
            conn,
            self.audit_table,
            action="user_created",
            actor_id=None,
            actor_kind="system",
            entity_type="user",
            entity_id=user_id,
        )
        return temporary

    async def set_permissions(
        self,
        conn: AsyncConnection,
        user_id: UUID,
        permissions: Iterable[Permission | str],
        *,
        actor_id: UUID | None,
    ) -> None:
        wanted = sorted({str(p) for p in permissions})
        unknown = [p for p in wanted if p not in ALL_PERMISSION_VALUES]
        if unknown:
            raise ValueError(f"unknown permissions: {unknown}")
        up = self.t.user_permissions
        await conn.execute(delete(up).where(up.c.user_id == user_id))
        if wanted:
            await conn.execute(insert(up), [{"user_id": user_id, "permission": p} for p in wanted])
        await write_audit(
            conn,
            self.audit_table,
            action="permissions_set",
            actor_id=actor_id,
            actor_kind="user" if actor_id else "system",
            entity_type="user",
            entity_id=user_id,
            detail={"permissions": wanted},
        )

    async def set_active(
        self, conn: AsyncConnection, user_id: UUID, active: bool, *, actor_id: UUID | None
    ) -> None:
        users = self.t.users
        await conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(is_active=active, updated_at=self.clock())
        )
        if not active:
            await self.revoke_sessions(conn, user_id)
        await write_audit(
            conn,
            self.audit_table,
            action="user_activated" if active else "user_deactivated",
            actor_id=actor_id,
            entity_type="user",
            entity_id=user_id,
        )

    async def reset_second_factor(
        self, conn: AsyncConnection, user_id: UUID, *, actor_id: UUID | None
    ) -> None:
        """Lost phone: the user enrolls again at next login."""
        users, rc = self.t.users, self.t.recovery_codes
        await conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(totp_secret_enc=None, totp_confirmed_at=None, totp_last_step=None)
        )
        await conn.execute(delete(rc).where(rc.c.user_id == user_id))
        await self.revoke_sessions(conn, user_id)
        await write_audit(
            conn,
            self.audit_table,
            action="totp_reset",
            actor_id=actor_id,
            entity_type="user",
            entity_id=user_id,
        )

    async def reset_password(
        self, conn: AsyncConnection, user_id: UUID, *, actor_id: UUID | None
    ) -> str:
        """Forgotten password: returns a temporary one the user must change."""
        temporary = passwords.generate_temporary_password()
        users = self.t.users
        await conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(
                password_hash=await asyncio.to_thread(passwords.hash_password, temporary),
                must_change_password=True,
                updated_at=self.clock(),
            )
        )
        await self.revoke_sessions(conn, user_id)
        await write_audit(
            conn,
            self.audit_table,
            action="password_reset",
            actor_id=actor_id,
            entity_type="user",
            entity_id=user_id,
        )
        return temporary
