"""Devices (D6, #19): pairing, bearer tokens, and the states a client Device moves through.

A client Device pairs once with a secret the admin screen offers (`offer`, `pair`), then names itself by an opaque
bearer token that `authenticate` alone judges. The Hub keeps only the SHA-256 of a secret or token and finds it by that
digest, so what a client sends is never compared with anything the Hub keeps. A client Device is

- active;
- dormant once unseen for longer than the dormant period, or after a restore. `approve` makes it active again with
  the same id and token and starts its dormant period over (#19 decision 6);
- revoked, for good, by the user or when its pairing secret comes back.

The Hub's own windows_hub row, which local import commits as (A7), has no token and is never paired, made dormant,
revoked or approved. Every change is recorded in the audit log. Each operation takes the time as `now`.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import math
import re
import secrets
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from importlib.metadata import version
from typing import TYPE_CHECKING, NewType

from campuslife_contracts import contract_root

from . import audit
from .audit import AuditEvent
from .clock import instant, now_instant
from .db import Snapshot, Tx

if TYPE_CHECKING:
    from .core import Core
    from .redaction import Redactor

log = logging.getLogger(__name__)

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


class DeviceState(enum.StrEnum):
    ACTIVE = "active"
    DORMANT = "dormant"
    REVOKED = "revoked"


class TokenStatus(enum.StrEnum):
    CURRENT = "current"
    PENDING = "pending"
    """Reissued and not used yet: its first use ends the current token (D23)."""


class ClientPlatform(enum.StrEnum):
    """The platforms that pair (device/pair_request); windows_hub is only the Hub's own row."""

    ANDROID = "android"
    IPADOS = "ipados"


HUB_DEVICE_NAME = "CampusLife Hub"
HUB_PLATFORM = "windows_hub"
DEFAULT_DORMANT_AFTER = timedelta(days=90)
OFFER_LIFETIME = timedelta(minutes=5)
PAIR_FAILURES_ALLOWED = 10
PAIR_FAILURE_WINDOW = timedelta(minutes=10)
TOUCH_EVERY = timedelta(hours=1)
"""last_seen_at and last_rejected_at are written only once they are older than this, so most requests only read."""

_TOKEN = json.loads((contract_root() / "schemas" / "device" / "pair_response.json").read_text(encoding="utf-8"))["$defs"]["token"]


class NoPublicUrl(Exception):
    pass


class UnknownDevice(LookupError):
    pass


class HubDeviceRefused(Exception):
    """The Hub's own Device is never revoked, approved or made dormant."""


class RevokedDevice(Exception):
    """A revoked Device is never approved; it pairs again as a new Device."""


@dataclass(frozen=True)
class PairingOffer:
    hub_url: str
    hub_id: str
    secret: str
    expires_at: datetime

    def wire(self) -> dict[str, str]:
        """The device/pairing_offer the QR code holds."""
        return {"hub_url": self.hub_url, "hub_id": self.hub_id, "secret": self.secret}


@dataclass(frozen=True)
class PairRequest:
    """A device/pair_request the sync port has checked against its schema."""

    secret: str
    platform: ClientPlatform
    device_name: str
    app_version: str


@dataclass(frozen=True)
class Paired:
    """A Device's id and the token it names itself by: the device/pair_response."""

    device_id: DeviceId
    token: str

    def wire(self) -> dict[str, str]:
        return {"device_id": self.device_id, "token": self.token}


@dataclass(frozen=True)
class DeviceRecord:
    id: DeviceId
    platform: str
    device_name: str
    app_version: str
    state: DeviceState
    created_at: str
    last_seen_at: str | None
    last_rejected_at: str | None
    last_status: Mapping[str, object] | None
    """The last device/status heartbeat, shown and never acted on (D1)."""
    last_status_at: str | None


class PairingLimiter:
    """Failed pairing attempts across every client, kept in memory only (#19 decision 7).

    One count for all: every request reaches the sync port from tailscale serve on 127.0.0.1, so there is no client
    address to count by."""

    def __init__(self, allowed: int = PAIR_FAILURES_ALLOWED, window: timedelta = PAIR_FAILURE_WINDOW):
        self._allowed = allowed
        self._window = window
        self._failures: deque[datetime] = deque()
        self.turn = asyncio.Lock()
        """Held by `pair` from the check to counting the failure, so attempts that arrive together cannot all pass the
        check before any failure is counted."""

    def retry_after(self, now: datetime) -> int | None:
        """Whole seconds until a pairing attempt is judged again, or None when it is judged now."""
        while self._failures and self._failures[0] + self._window <= now:
            self._failures.popleft()
        if len(self._failures) < self._allowed:
            return None
        frees_at = self._failures[len(self._failures) - self._allowed] + self._window
        return max(1, math.ceil((frees_at - now).total_seconds()))

    def failed(self, now: datetime) -> None:
        self._failures.append(now)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _offer(tx: Tx, *, secret_sha256: str, now: datetime) -> None:
    tx.execute("DELETE FROM pairing_secrets WHERE used_at IS NULL")
    tx.execute(
        "INSERT INTO pairing_secrets (secret_sha256, created_at, expires_at) VALUES (?, ?, ?)",
        (secret_sha256, instant(now), instant(now + OFFER_LIFETIME)),
    )


async def offer(core: Core, *, now: datetime, public_url: str | None, redactor: Redactor) -> PairingOffer:
    """A new pairing offer, good once for OFFER_LIFETIME; it ends the offer before it if that was not used."""
    if public_url is None:
        raise NoPublicUrl("set serve --public-url to the Hub's https://<name>.<tailnet>.ts.net before adding a device")
    secret = redactor.register(secrets.token_hex(16))
    await core.db.write(partial(_offer, secret_sha256=_sha256(secret), now=now))
    return PairingOffer(public_url, core.identity.hub_id, secret, now + OFFER_LIFETIME)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class _Pairing:
    device: DeviceId | None
    """The Device paired, or None when the secret was refused."""
    revoked: DeviceId | None = None
    """The Device whose secret came back, revoked now or before, whose uploads end."""


def _pair(tx: Tx, *, secret_sha256: str, request: PairRequest, token_sha256: str, now: datetime, seen_as: dict[str, str]) -> _Pairing:
    row = tx.execute("SELECT expires_at, device_id FROM pairing_secrets WHERE secret_sha256 = ?", (secret_sha256,)).fetchone()
    if row is not None and row[1] is not None:
        _revoke(tx, device=row[1], now=now, reason="pairing_secret_reused")
        audit.record(tx, AuditEvent.PAIR_FAILED, at=now, subject=row[1], details={"reason": "secret_reused", **seen_as})
        return _Pairing(None, DeviceId(row[1]))
    if row is None or row[0] <= instant(now):
        audit.record(tx, AuditEvent.PAIR_FAILED, at=now, details={"reason": "unknown_secret" if row is None else "expired_secret", **seen_as})
        return _Pairing(None)
    device = DeviceId(str(uuid.uuid4()))
    tx.execute(
        "INSERT INTO devices (id, platform, device_name, app_version, state, created_at) VALUES (?, ?, ?, ?, 'active', ?)",
        (device, str(request.platform), request.device_name, request.app_version, instant(now)),
    )
    tx.execute("INSERT INTO device_tokens (token_sha256, device_id, status, created_at) VALUES (?, ?, 'current', ?)", (token_sha256, device, instant(now)))
    tx.execute("UPDATE pairing_secrets SET used_at = ?, device_id = ? WHERE secret_sha256 = ?", (instant(now), device, secret_sha256))
    audit.record(tx, AuditEvent.PAIR, at=now, subject=device, details={"platform": str(request.platform), **seen_as})
    return _Pairing(device)


async def pair(
    core: Core, request: PairRequest, *, now: datetime, limiter: PairingLimiter, redactor: Redactor, tailscale_user: str | None = None
) -> Paired | Refusal:
    """Pair a new Device with an offered secret, used once before its offer expires.

    A secret used before is refused and revokes the Device it paired (D6), since one of the two holders is not the
    user's. `tailscale_user` is the Tailscale-User-Login the request came with: the audit log keeps it, nothing trusts it.
    """
    ClientPlatform(request.platform)
    token = _new_token()
    seen_as = {} if tailscale_user is None else {"tailscale_user": tailscale_user}
    async with limiter.turn:
        retry_after = limiter.retry_after(now)
        if retry_after is not None:
            return Refusal(RefusalCode.UNAUTHORIZED, retry_after)
        pairing = await core.db.write(
            partial(_pair, secret_sha256=_sha256(request.secret), request=request, token_sha256=_sha256(token), now=now, seen_as=seen_as)
        )
        if pairing.revoked is not None:
            await _end_uploads(core, pairing.revoked)
        if pairing.device is None:
            limiter.failed(now)
            return Refusal(RefusalCode.UNAUTHORIZED)
    return Paired(pairing.device, redactor.register(token))


class _Change(enum.Enum):
    SEEN = enum.auto()
    REJECTED = enum.auto()
    DORMANT = enum.auto()
    RETIRE = enum.auto()
    """A pending token's first use: it becomes current and the current token ends."""


@dataclass(frozen=True)
class _Verdict:
    answer: DeviceId | Refusal
    device: DeviceId | None = None
    change: _Change | None = None
    """What answering writes; most answers write nothing."""


def _unseen_since(now: datetime, dormant_after: timedelta) -> str:
    """A Device is dormant once coalesce(last_seen_at, created_at) is before this."""
    return instant(now - dormant_after)


def _touched_within(at: str | None, now: datetime) -> bool:
    return at is not None and at >= instant(now - TOUCH_EVERY)


def _judge(runner: Tx | Snapshot, *, token_sha256: str, now: datetime, dormant_after: timedelta) -> _Verdict:
    row = runner.execute(
        "SELECT d.id, d.state, d.created_at, d.last_seen_at, d.last_rejected_at, t.status"
        " FROM device_tokens AS t JOIN devices AS d ON d.id = t.device_id WHERE t.token_sha256 = ?",
        (token_sha256,),
    ).fetchone()
    if row is None:
        return _Verdict(Refusal(RefusalCode.UNAUTHORIZED))
    device, state, created_at, last_seen_at, last_rejected_at, status = row
    device = DeviceId(device)
    rejected = None if _touched_within(last_rejected_at, now) else _Change.REJECTED
    if state == DeviceState.REVOKED:
        return _Verdict(Refusal(RefusalCode.DEVICE_REVOKED), device, rejected)
    if state == DeviceState.DORMANT:
        return _Verdict(Refusal(RefusalCode.DEVICE_DORMANT), device, rejected)
    if (last_seen_at or created_at) < _unseen_since(now, dormant_after):
        return _Verdict(Refusal(RefusalCode.DEVICE_DORMANT), device, _Change.DORMANT)
    if status == TokenStatus.PENDING:
        return _Verdict(device, device, _Change.RETIRE)
    return _Verdict(device, device, None if _touched_within(last_seen_at, now) else _Change.SEEN)


def _judge_and_write(tx: Tx, *, token_sha256: str, now: datetime, dormant_after: timedelta) -> _Verdict:
    verdict = _judge(tx, token_sha256=token_sha256, now=now, dormant_after=dormant_after)
    if verdict.change is _Change.SEEN:
        tx.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (instant(now), verdict.device))
    elif verdict.change is _Change.REJECTED:
        tx.execute("UPDATE devices SET last_rejected_at = ? WHERE id = ?", (instant(now), verdict.device))
    elif verdict.change is _Change.DORMANT:
        tx.execute("UPDATE devices SET state = 'dormant', last_rejected_at = ? WHERE id = ?", (instant(now), verdict.device))
        audit.record(tx, AuditEvent.DORMANT, at=now, subject=verdict.device, details={"reason": "inactive"})
    elif verdict.change is _Change.RETIRE:
        tx.execute("DELETE FROM device_tokens WHERE device_id = ? AND status = 'current'", (verdict.device,))
        tx.execute("UPDATE device_tokens SET status = 'current' WHERE token_sha256 = ?", (token_sha256,))
        tx.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (instant(now), verdict.device))
        audit.record(tx, AuditEvent.TOKEN_RETIRED, at=now, subject=verdict.device)
    return verdict


async def authenticate(core: Core, bearer: str | None, *, now: datetime, redactor: Redactor, dormant_after: timedelta = DEFAULT_DORMANT_AFTER) -> DeviceId | Refusal:
    """The Device a bearer token names, or why it names none: the one place a token is judged (#19 decision 7)."""
    if bearer is None or len(bearer) > _TOKEN["maxLength"] or not re.fullmatch(_TOKEN["pattern"], bearer):
        return Refusal(RefusalCode.UNAUTHORIZED)
    judge = partial(_judge, token_sha256=_sha256(bearer), now=now, dormant_after=dormant_after)
    verdict = await core.db.read(judge)
    if verdict.change is not None:
        verdict = await core.db.write(partial(_judge_and_write, **judge.keywords))
    if not isinstance(verdict.answer, Refusal):
        redactor.register(bearer)
    return verdict.answer


def _reissue(tx: Tx, *, device: str, token_sha256: str, now: datetime) -> None:
    tx.execute("DELETE FROM device_tokens WHERE device_id = ? AND status = 'pending'", (device,))
    tx.execute("INSERT INTO device_tokens (token_sha256, device_id, status, created_at) VALUES (?, ?, 'pending', ?)", (token_sha256, device, instant(now)))
    audit.record(tx, AuditEvent.REISSUE, at=now, subject=device)


async def reissue(core: Core, device: str, *, now: datetime, redactor: Redactor) -> Paired:
    """Another token for a Device that authenticated (D23, POST /v1/devices/me/token).

    The token it holds keeps working until the new one is first used. Asking again issues another token and ends the
    one not used yet: the Hub keeps only SHA-256 of a token, so it cannot hand the same one out again (#19 decision 2).
    """
    token = _new_token()
    await core.db.write(partial(_reissue, device=device, token_sha256=_sha256(token), now=now))
    return Paired(DeviceId(device), redactor.register(token))


def _mark_dormant(tx: Tx, *, now: datetime, dormant_after: timedelta) -> list[DeviceId]:
    made_dormant = tx.execute(
        "UPDATE devices SET state = 'dormant' WHERE platform != ? AND state = 'active' AND coalesce(last_seen_at, created_at) < ? RETURNING id",
        (HUB_PLATFORM, _unseen_since(now, dormant_after)),
    )
    devices = sorted(DeviceId(device) for (device,) in made_dormant)
    for device in devices:
        audit.record(tx, AuditEvent.DORMANT, at=now, subject=device, details={"reason": "inactive"})
    return devices


async def mark_dormant(core: Core, *, now: datetime, dormant_after: timedelta = DEFAULT_DORMANT_AFTER) -> list[DeviceId]:
    """Make every active client Device unseen for longer than `dormant_after` dormant, as authenticating it would."""
    return await core.db.write(partial(_mark_dormant, now=now, dormant_after=dormant_after))


def _client_state(tx: Tx, device: str) -> DeviceState:
    row = tx.execute("SELECT platform, state FROM devices WHERE id = ?", (device,)).fetchone()
    if row is None:
        raise UnknownDevice(f"the Hub knows no device {device}")
    if row[0] == HUB_PLATFORM:
        raise HubDeviceRefused("the Hub's own device is never revoked, approved or made dormant")
    return DeviceState(row[1])


def _revoke(tx: Tx, *, device: str, now: datetime, reason: str) -> bool:
    if _client_state(tx, device) == DeviceState.REVOKED:
        return False
    tx.execute("UPDATE devices SET state = 'revoked' WHERE id = ?", (device,))
    audit.record(tx, AuditEvent.REVOKE, at=now, subject=device, details={"reason": reason})
    return True


async def _end_uploads(core: Core, device: DeviceId) -> None:
    for leftover in await core.blobs.drop_sessions(device):
        log.warning("left %s for a later clean-up: %s", leftover.path, leftover.reason)


async def revoke(core: Core, device: str, *, now: datetime) -> bool:
    """Revoke a client Device for good and end its uploads; whether it was not revoked before.

    Requests it already had authenticated run to their end. A revoke repeated after a crash ends the uploads the
    first one left."""
    revoked = await core.db.write(partial(_revoke, device=device, now=now, reason="user"))
    await _end_uploads(core, DeviceId(device))
    return revoked


def _approve(tx: Tx, *, device: str, now: datetime) -> bool:
    state = _client_state(tx, device)
    if state == DeviceState.REVOKED:
        raise RevokedDevice(f"device {device} is revoked; pair it again as a new device")
    if state == DeviceState.ACTIVE:
        return False
    tx.execute("UPDATE devices SET state = 'active', last_seen_at = ? WHERE id = ?", (instant(now), device))
    audit.record(tx, AuditEvent.APPROVE, at=now, subject=device)
    return True


async def approve(core: Core, device: str, *, now: datetime) -> bool:
    """Make a dormant Device active again with the same id and token, its dormant period starting over at `now`
    (#19 decision 6); whether it was dormant."""
    return await core.db.write(partial(_approve, device=device, now=now))


def _heartbeat(tx: Tx, *, device: str, status: Mapping[str, object], now: datetime) -> None:
    tx.execute(
        "UPDATE devices SET last_status = ?, last_status_at = ?, app_version = ? WHERE id = ?",
        (json.dumps(dict(status), ensure_ascii=False, sort_keys=True), instant(now), status["app_version"], device),
    )


async def heartbeat(core: Core, device: str, status: Mapping[str, object], *, now: datetime) -> None:
    """Keep a Device's device/status heartbeat (POST /v1/devices/me/status), the app_version it reports included."""
    await core.db.write(partial(_heartbeat, device=device, status=status, now=now))


def device(runner: Tx | Snapshot, device_id: str) -> DeviceRecord | None:
    row = runner.execute(
        "SELECT id, platform, device_name, app_version, state, created_at, last_seen_at, last_rejected_at, last_status, last_status_at"
        " FROM devices WHERE id = ?",
        (device_id,),
    ).fetchone()
    if row is None:
        return None
    device_id, platform, name, app_version, state, created_at, last_seen_at, last_rejected_at, last_status, last_status_at = row
    status = None if last_status is None else json.loads(last_status)
    return DeviceRecord(DeviceId(device_id), platform, name, app_version, DeviceState(state), created_at, last_seen_at, last_rejected_at, status, last_status_at)


def ensure_hub_device(tx: Tx) -> DeviceId:
    tx.execute(
        "INSERT INTO devices (id, platform, device_name, app_version, state, created_at)"
        " VALUES (?, 'windows_hub', ?, ?, 'active', ?) ON CONFLICT DO NOTHING",
        (str(uuid.uuid4()), HUB_DEVICE_NAME, version("campuslife-hub"), now_instant()),
    )
    (device_id,) = tx.execute("SELECT id FROM devices WHERE platform = 'windows_hub'").fetchone()
    return DeviceId(device_id)
