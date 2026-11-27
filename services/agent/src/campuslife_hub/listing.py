"""Read-only summaries of what the Hub holds, for `campuslife-hub ls` and the local UI."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .db import Snapshot


@dataclass(frozen=True)
class AttachmentSummary:
    id: str
    blob_sha256: str
    original_filename: str | None
    cloud_policy: str


@dataclass(frozen=True)
class CaptureSummary:
    id: str
    created_at: str
    origin_type: str
    attachments: list[AttachmentSummary]


CAPTURES_BY_REV = (
    "SELECT captures.data FROM captures JOIN sync_index ON sync_index.entity = 'capture' AND sync_index.id = captures.id"
)


def _summary(snap: Snapshot, capture_json: str) -> CaptureSummary:
    capture = json.loads(capture_json)
    attachments = [
        json.loads(data)
        for (data,) in snap.execute(
            "SELECT data FROM attachments WHERE capture_id = ? ORDER BY json_extract(data, '$.original_filename')", (capture["id"],)
        )
    ]
    return CaptureSummary(
        capture["id"],
        capture["created_at"],
        capture["origin"]["type"],
        [AttachmentSummary(a["id"], a["blob_sha256"], a.get("original_filename"), a["cloud_policy"]) for a in attachments],
    )


def list_captures(snap: Snapshot) -> list[CaptureSummary]:
    """Captures in the order the Hub took them in, each with its Attachments."""
    return [_summary(snap, data) for (data,) in snap.execute(f"{CAPTURES_BY_REV} ORDER BY sync_index.rev")]


def latest_captures(snap: Snapshot, limit: int) -> list[CaptureSummary]:
    """The limit Captures the Hub took in last, the last first."""
    return [_summary(snap, data) for (data,) in snap.execute(f"{CAPTURES_BY_REV} ORDER BY sync_index.rev DESC LIMIT ?", (limit,))]


@dataclass(frozen=True)
class LectureSessionSummary:
    id: str
    title: str
    date: str
    course: str | None


SESSIONS = (
    "SELECT sessions.data, courses.data FROM items AS sessions"
    " LEFT JOIN items AS courses ON courses.id = json_extract(sessions.data, '$.fields.course_id')"
    " WHERE json_extract(sessions.data, '$.kind') = 'lecture_session' AND json_extract(sessions.data, '$.deleted_at') IS NULL"
)


def _session(session_json: str, course_json: str | None) -> LectureSessionSummary:
    session, course = json.loads(session_json), json.loads(course_json) if course_json else None
    return LectureSessionSummary(session["id"], session["title"], session["fields"]["date"]["date"], course["title"] if course else None)


def lecture_sessions(snap: Snapshot, limit: int) -> list[LectureSessionSummary]:
    """The limit lecture sessions not in the trash, the latest date first, for the import form to link files to."""
    rows = snap.execute(f"{SESSIONS} ORDER BY json_extract(sessions.data, '$.fields.date.date') DESC, sessions.rowid DESC LIMIT ?", (limit,))
    return [_session(*row) for row in rows]


def lecture_session(snap: Snapshot, item_id: str) -> LectureSessionSummary | None:
    """The lecture session with this id, None when there is none or it is in the trash."""
    row = snap.execute(f"{SESSIONS} AND sessions.id = ?", (item_id,)).fetchone()
    return None if row is None else _session(*row)
