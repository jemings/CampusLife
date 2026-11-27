import sqlite3
from datetime import UTC, datetime

import pytest

from campuslife_hub import audit
from campuslife_hub.audit import AuditEntry, AuditEvent

AT = datetime(2026, 11, 27, 9, 30, 0, 250000, tzinfo=UTC)


async def test_records_read_back_in_the_order_they_were_written(db):
    await db.write(lambda tx: audit.record(tx, AuditEvent.REVOKE, at=AT, subject="device-1", details={"reason": "admin"}))
    await db.write(lambda tx: audit.record(tx, AuditEvent.PAIR_FAILED, at=AT, details={"reason": "expired_secret"}))
    assert await db.read(audit.entries) == [
        AuditEntry(1, "2026-11-27T09:30:00.250Z", AuditEvent.REVOKE, "device-1", {"reason": "admin"}),
        AuditEntry(2, "2026-11-27T09:30:00.250Z", AuditEvent.PAIR_FAILED, None, {"reason": "expired_secret"}),
    ]


async def test_entries_after_a_sequence_number_leave_out_the_earlier_ones(db):
    for subject in ("a", "b", "c"):
        await db.write(lambda tx, subject=subject: audit.record(tx, AuditEvent.APPROVE, at=AT, subject=subject))
    assert [entry.subject for entry in await db.read(lambda snap: audit.entries(snap, after=1))] == ["b", "c"]


@pytest.mark.parametrize("statement", ["UPDATE audit_log SET event = 'approve'", "DELETE FROM audit_log"])
async def test_the_audit_log_is_append_only(db, statement):
    """D3: the Hub's own record of what happened to devices; nothing rewrites or removes it."""
    await db.write(lambda tx: audit.record(tx, AuditEvent.REVOKE, at=AT, subject="device-1"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        await db.write(lambda tx: tx.execute(statement))
    assert len(await db.read(audit.entries)) == 1

