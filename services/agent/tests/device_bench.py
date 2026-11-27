"""What the device tests share: a Hub, a clock the test moves, and the device operations bound to both."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from sync_client import CONTRACT, Waking, epoch_of
from sync_contract import checked

from campuslife_hub import devices
from campuslife_hub.core import Core, open_core
from campuslife_hub.devices import ClientPlatform, DeviceSettings, PairingLimiter, PairRequest
from campuslife_hub.host import Hosted
from campuslife_hub.redaction import Redactor
from campuslife_hub.sync import create_sync_app
from campuslife_hub.sync_devices import DeviceSync

PUBLIC_URL = "https://campus-laptop.tail0a1b2c.ts.net"
START = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
UNKNOWN_SECRET = "0000000000000000aaaaaaaaaaaaaaaa"


class Clock:
    """A wall clock, and the monotonic clock beside it, that move only when the test says."""

    def __init__(self, at: datetime = START):
        self.at = at
        self.monotonic = 0.0

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> datetime:
        """Let time pass: both clocks move."""
        step = timedelta(**delta)
        self.at += step
        self.monotonic += step.total_seconds()
        return self.at

    def set_wall(self, at: datetime) -> None:
        """Set the wall clock alone, as a user or a time sync does."""
        self.at = at


def request(secret: str, *, platform=ClientPlatform.ANDROID, name="Fold8", version="0.1.0") -> PairRequest:
    return PairRequest(secret=secret, platform=platform, device_name=name, app_version=version)


@dataclass
class Bench:
    core: Core
    clock: Clock
    redactor: Redactor = field(default_factory=Redactor)
    limiter: PairingLimiter | None = None
    dormant_after: timedelta = devices.DEFAULT_DORMANT_AFTER

    def __post_init__(self):
        if self.limiter is None:
            self.limiter = PairingLimiter(clock=lambda: self.clock.monotonic)

    async def offer(self):
        return await devices.offer(self.core, now=self.clock(), public_url=PUBLIC_URL, redactor=self.redactor)

    async def pair(self, secret: str, **fields):
        return await devices.pair(self.core, request(secret, **fields), now=self.clock(), limiter=self.limiter, redactor=self.redactor)

    async def paired(self, **fields) -> devices.Paired:
        offer = await self.offer()
        return await self.pair(offer.secret, **fields)

    async def authenticate(self, bearer: str | None):
        return await devices.authenticate(self.core, bearer, now=self.clock(), redactor=self.redactor, dormant_after=self.dormant_after)

    async def reissue(self, device_id: str) -> devices.Paired:
        return await devices.reissue(self.core, device_id, now=self.clock(), redactor=self.redactor)

    async def revoke(self, device_id: str) -> bool:
        return await devices.revoke(self.core, device_id, now=self.clock())

    async def approve(self, device_id: str) -> bool:
        return await devices.approve(self.core, device_id, now=self.clock())

    async def device(self, device_id: str) -> devices.DeviceRecord:
        return await self.core.db.read(lambda snap: devices.device(snap, device_id))

    @asynccontextmanager
    async def sync(self):
        """A client of the sync app as serve makes it, on this bench's Hub, clock and limiter. It sends no bearer
        token of its own, sends the Hub's epoch, and every response it gets has passed sync_contract.check."""
        hosted = Hosted(self.core, Waking(), self.redactor, DeviceSettings(dormant_after=self.dormant_after))
        device_sync = DeviceSync(hosted, clock=self.clock, limiter=self.limiter)
        app = create_sync_app(hosted, device_sync.authenticate, [device_sync.router])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub.tailnet.test", headers={**CONTRACT, **epoch_of(self.core)},
            event_hooks={"response": [checked]},
        ) as client:
            yield client


@asynccontextmanager
async def bench(tmp_path, *, clock: Clock | None = None, root: str = "root", **fields):
    """The Hub at tmp_path/root, made on first use; opening the same root again keeps what it holds."""
    async with open_core(tmp_path / root, create=True, sync_roots=[]) as core:
        yield Bench(core, clock or Clock(), **fields)
