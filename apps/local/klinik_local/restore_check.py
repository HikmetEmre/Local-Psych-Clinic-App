"""Checks that a restored backup is usable (brief §12, restore test).

A backup is only good if it restores AND its encrypted text can still be read with
the keys we hold. This connects to a restored copy (never the live database) and:
- confirms the schema is at the expected migration;
- counts the main tables;
- decrypts a sample of client names and session notes with LOCAL_FERNET_KEYS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
from alembic.config import Config
from alembic.script import ScriptDirectory

from klinik_local.crypto.fernet import DecryptionError, FieldCipher

SAMPLE = 50
ROOT = Path(__file__).resolve().parents[3]


@dataclass
class RestoreReport:
    schema_version: str | None = None
    expected_version: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    names_checked: int = 0
    notes_checked: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def lines(self) -> list[str]:
        out = [
            f"schema version: {self.schema_version} (expected {self.expected_version})",
            *(f"{table}: {count}" for table, count in self.counts.items()),
            f"client names decrypted: {self.names_checked}",
            f"session notes decrypted: {self.notes_checked}",
        ]
        out += [f"PROBLEM: {p}" for p in self.problems]
        out.append("RESULT: OK" if self.ok else "RESULT: FAILED")
        return out


def expected_head() -> str | None:
    import os

    configured = os.environ.get("KLINIK_MIGRATIONS_DIR")
    script_location = Path(configured) if configured else ROOT / "migrations" / "local"
    if not script_location.exists():  # installed without the source tree
        return None
    config = Config()
    config.set_main_option("script_location", str(script_location))
    return ScriptDirectory.from_config(config).get_current_head()


async def verify_restore(dsn: str, fernet_keys: str | None) -> RestoreReport:
    report = RestoreReport(expected_version=expected_head())
    try:
        cipher = FieldCipher(fernet_keys)
    except ValueError as exc:
        report.problems.append(f"LOCAL_FERNET_KEYS: {exc}")
        return report
    conn = await asyncpg.connect(dsn)
    try:
        report.schema_version = await conn.fetchval("SELECT version_num FROM alembic_version")
        if report.expected_version and report.schema_version != report.expected_version:
            report.problems.append("schema version differs from this software's migrations")
        for table in (
            "users",
            "clients",
            "therapy_sessions",
            "appointments",
            "payments",
            "audit_log",
        ):
            report.counts[table] = int(await conn.fetchval(f"SELECT count(*) FROM {table}"))  # noqa: S608
        if report.counts["users"] == 0:
            report.problems.append("no users in the restored database")
        for row in await conn.fetch(
            "SELECT full_name_encrypted FROM clients ORDER BY random() LIMIT $1", SAMPLE
        ):
            try:
                cipher.decrypt(row[0])
                report.names_checked += 1
            except DecryptionError:
                report.problems.append("a client name cannot be decrypted with the current keys")
                break
        for row in await conn.fetch(
            "SELECT notes_encrypted FROM therapy_sessions WHERE notes_encrypted IS NOT NULL "
            "ORDER BY random() LIMIT $1",
            SAMPLE,
        ):
            try:
                cipher.decrypt(row[0])
                report.notes_checked += 1
            except DecryptionError:
                report.problems.append("a session note cannot be decrypted with the current keys")
                break
    finally:
        await conn.close()
    return report
