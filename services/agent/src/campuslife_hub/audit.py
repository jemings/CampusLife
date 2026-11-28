"""The Hub's audit log (D3, #19 decision 3): an append-only record of what happened that the Hub answers for.

It is never synced and holds no token or secret. #19 records pairing, revokes, approvals, dormancy and token
reissues, #18 the resolution of ConflictRecords; #8 adds its events to AuditEvent.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .clock import instant
from .db import Snapshot, Tx


class AuditEvent(enum.StrEnum):
    PAIR = "pair"
    PAIR_FAILED = "pair_failed"
    REVOKE = "revoke"
    APPROVE = "approve"
    DORMANT = "dormant"
    REISSUE = "reissue"
    TOKEN_RETIRED = "token_retired"
    """A reissued token's first use ended the token before it."""
    CONFLICT_RESOLVED = "conflict_resolved"
    """A conflict.resolve resolved an open ConflictRecord; details name the record, its entity and the Mutation (D24)."""


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    at: str
    event: AuditEvent
    subject: str | None
    details: Mapping[str, object]


def record(tx: Tx, event: AuditEvent, *, at: datetime, subject: str | None = None, details: Mapping[str, object] | None = None) -> int:
    """Append one entry; its sequence number."""
    cursor = tx.execute(
        "INSERT INTO audit_log (at, event, subject, details_json) VALUES (?, ?, ?, ?)",
        (instant(at), str(event), subject, json.dumps(dict(details or {}), ensure_ascii=False, sort_keys=True)),
    )
    return cursor.lastrowid


def entries(runner: Tx | Snapshot, *, after: int = 0) -> list[AuditEntry]:
    """The entries after sequence number `after`, oldest first."""
    rows = runner.execute("SELECT seq, at, event, subject, details_json FROM audit_log WHERE seq > ? ORDER BY seq", (after,))
    return [AuditEntry(seq, at, AuditEvent(event), subject, json.loads(details)) for seq, at, event, subject, details in rows]
