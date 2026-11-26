"""hub_id, issued once at install, and epoch, issued again on every restore (D3)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .db import START_THE_HUB, NotInitialized, Snapshot, Tx


@dataclass(frozen=True)
class HubIdentity:
    hub_id: str
    epoch: str


def read_identity(runner: Tx | Snapshot) -> HubIdentity:
    row = runner.execute("SELECT hub_id, epoch FROM hub_state").fetchone()
    if row is None:
        raise NotInitialized(f"the Hub has no identity yet; {START_THE_HUB}")
    return HubIdentity(*row)


def ensure_identity(tx: Tx) -> HubIdentity:
    tx.execute(
        "INSERT INTO hub_state (singleton, hub_id, epoch) VALUES (1, ?, ?) ON CONFLICT (singleton) DO NOTHING",
        (str(uuid.uuid4()), str(uuid.uuid4())),
    )
    return read_identity(tx)
