"""The Inbox (#9 decision 1, DOMAIN-MODEL §4): computed from what the Hub holds, never stored.

Inbox = the open Suggestions and the unclassified Captures. A Capture is unclassified while no live Relation has, at
either end, the Capture, one of its Attachments or one of their Blobs. The Relation's type does not matter, so a
Relation between two Blobs classifies a Capture, and so does one that reaches a Blob another Capture shares (D2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .db import Snapshot

OPEN_SUGGESTIONS = (
    "SELECT suggestions.data FROM suggestions JOIN sync_index ON sync_index.entity = 'suggestion' AND sync_index.id = suggestions.id"
    " WHERE suggestions.state = 'open' ORDER BY sync_index.rev DESC"
)
CAPTURES = (
    "SELECT captures.data FROM captures JOIN sync_index ON sync_index.entity = 'capture' AND sync_index.id = captures.id"
    " ORDER BY sync_index.rev DESC"
)


@dataclass(frozen=True)
class Inbox:
    suggestions: list[dict]
    """Open Suggestion rows, the last written first."""
    captures: list[dict]
    """Unclassified Capture rows, the last taken in first."""


def _anchors(capture: dict, attachments: list[dict]) -> set[str]:
    return {
        f"capture:{capture['id']}",
        *(f"attachment:{attachment['id']}" for attachment in attachments),
        *(f"blob:{attachment['blob_sha256']}" for attachment in attachments),
    }


def compute(snap: Snapshot) -> Inbox:
    suggestions = [json.loads(data) for (data,) in snap.execute(OPEN_SUGGESTIONS)]
    related = {end for row in snap.execute("SELECT json_extract(data, '$.from'), json_extract(data, '$.to') FROM relations") for end in row}
    attachments: dict[str, list[dict]] = {}
    for (data,) in snap.execute("SELECT data FROM attachments"):
        attachment = json.loads(data)
        attachments.setdefault(attachment["capture_id"], []).append(attachment)
    captures = [
        capture
        for capture in (json.loads(data) for (data,) in snap.execute(CAPTURES))
        if related.isdisjoint(_anchors(capture, attachments.get(capture["id"], [])))
    ]
    return Inbox(suggestions, captures)
