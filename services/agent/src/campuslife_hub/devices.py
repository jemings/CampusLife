"""Device rows (D6). PR1 has only the Hub's own windows_hub row, which local import commits as (A7)."""

from __future__ import annotations

import uuid
from importlib.metadata import version
from typing import NewType

from .clock import now_instant
from .db import Tx

DeviceId = NewType("DeviceId", str)

HUB_DEVICE_NAME = "CampusLife Hub"


def ensure_hub_device(tx: Tx) -> DeviceId:
    tx.execute(
        "INSERT INTO devices (id, platform, device_name, app_version, state, created_at)"
        " VALUES (?, 'windows_hub', ?, ?, 'active', ?) ON CONFLICT DO NOTHING",
        (str(uuid.uuid4()), HUB_DEVICE_NAME, version("campuslife-hub"), now_instant()),
    )
    (device_id,) = tx.execute("SELECT id FROM devices WHERE platform = 'windows_hub'").fetchone()
    return DeviceId(device_id)
