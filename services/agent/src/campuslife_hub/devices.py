"""Device rows (D6). PR1 has only the Hub's own windows_hub row, which local import commits as (A7).

A sync request names its Device by its bearer token: authenticating it gives the DeviceId or a Refusal (#4, #19).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from importlib.metadata import version
from typing import NewType

from .clock import now_instant
from .db import Tx

DeviceId = NewType("DeviceId", str)


class RefusalCode(enum.StrEnum):
    """Why a bearer token names no Device: the codes of a 401 (sync.yaml, D6)."""

    UNAUTHORIZED = "unauthorized"
    DEVICE_REVOKED = "device_revoked"
    DEVICE_DORMANT = "device_dormant"


@dataclass(frozen=True)
class Refusal:
    code: RefusalCode
    retry_after: int | None = None
    """Whole seconds to wait before asking again, sent as Retry-After."""


HUB_DEVICE_NAME = "CampusLife Hub"


def ensure_hub_device(tx: Tx) -> DeviceId:
    tx.execute(
        "INSERT INTO devices (id, platform, device_name, app_version, state, created_at)"
        " VALUES (?, 'windows_hub', ?, ?, 'active', ?) ON CONFLICT DO NOTHING",
        (str(uuid.uuid4()), HUB_DEVICE_NAME, version("campuslife-hub"), now_instant()),
    )
    (device_id,) = tx.execute("SELECT id FROM devices WHERE platform = 'windows_hub'").fetchone()
    return DeviceId(device_id)
