"""Read-only summaries for `campuslife-hub ls`."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .db import Snapshot


@dataclass(frozen=True)
class AttachmentSummary:
    blob_sha256: str
    original_filename: str | None
    cloud_policy: str


@dataclass(frozen=True)
class CaptureSummary:
    id: str
    created_at: str
    origin_type: str
    attachments: list[AttachmentSummary]


def list_captures(snap: Snapshot) -> list[CaptureSummary]:
    """Captures in the order the Hub took them in, each with its Attachments."""
    captures = snap.execute(
        "SELECT captures.data FROM captures JOIN sync_index ON sync_index.entity = 'capture' AND sync_index.id = captures.id"
        " ORDER BY sync_index.rev"
    ).fetchall()
    summaries = []
    for (capture_json,) in captures:
        capture = json.loads(capture_json)
        attachments = [
            json.loads(data)
            for (data,) in snap.execute(
                "SELECT data FROM attachments WHERE capture_id = ? ORDER BY json_extract(data, '$.original_filename')", (capture["id"],)
            )
        ]
        summaries.append(
            CaptureSummary(
                capture["id"],
                capture["created_at"],
                capture["origin"]["type"],
                [AttachmentSummary(a["blob_sha256"], a.get("original_filename"), a["cloud_policy"]) for a in attachments],
            )
        )
    return summaries
