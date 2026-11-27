"""What the sync app's tests share: a Hub with a paired phone and a client whose every response sync_contract checks."""

import dataclasses
import uuid
from contextlib import asynccontextmanager

import httpx
from campuslife_contracts import CONTRACT_VERSION
from sync_contract import checked

from campuslife_hub.blobs import BlobStore
from campuslife_hub.clock import now_instant
from campuslife_hub.core import open_core
from campuslife_hub.devices import DeviceId, Refusal, RefusalCode
from campuslife_hub.host import Hosted
from campuslife_hub.redaction import Redactor
from campuslife_hub.sync import create_sync_app

TOKEN = "phone-token-" + "x" * 40
CONTRACT = {"CampusLife-Contract": str(CONTRACT_VERSION)}


class Waking:
    """The job runner as the sync app sees it: something to wake once a commit leaves jobs to run."""

    def __init__(self):
        self.wakes = 0

    def wake(self):
        self.wakes += 1


def admitting(device):
    """An authenticator for tests: TOKEN is the device, every other bearer is refused as unknown."""

    async def authenticate(bearer):
        return device if bearer == TOKEN else Refusal(RefusalCode.UNAUTHORIZED)

    return authenticate


async def add_phone(core) -> DeviceId:
    device = str(uuid.uuid4())
    await core.db.write(
        lambda tx: tx.execute(
            "INSERT INTO devices (id, platform, device_name, app_version, state, created_at) VALUES (?, 'android', 'phone', '0.1.0', 'active', ?)",
            (device, now_instant()),
        )
    )
    return DeviceId(device)


def client_of(app, headers=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://hub.tailnet.test",
        headers={**CONTRACT, "Authorization": f"Bearer {TOKEN}", **(headers or {})},
        event_hooks={"response": [checked]},
    )


def request_with(client, method, path, replaced):
    """A request with the client's headers, each header named in replaced sent once per value given instead."""
    kept = [(name, value) for name, value in client.headers.multi_items() if name.lower() not in replaced]
    added = [(name, value) for name, values in replaced.items() for value in values]
    return httpx.Request(method, client.base_url.join(path), headers=[*kept, *added])


@asynccontextmanager
async def syncing(tmp_path, *, authenticate=None, routers=(), phone=None, chunk_size=None, free_space=None, **options):
    """The sync app over the Hub at tmp_path/root, a client that sends the phone's token, and the runner it wakes.

    The Hub and the phone are made on first use; pass the phone again to reopen the same Hub as the same phone.
    chunk_size and free_space replace the Blob store's own, so a test can upload in small chunks or run out of room.
    """
    async with open_core(tmp_path / "root", create=True, sync_roots=[]) as core:
        phone = phone or await add_phone(core)
        if chunk_size is not None or free_space is not None:
            blobs = BlobStore(
                core.db, blobs_dir=core.layout.blobs, staging_dir=core.layout.staging, uploads_dir=core.layout.uploads,
                **({} if chunk_size is None else {"chunk_size": chunk_size}), **({} if free_space is None else {"free_space": free_space}),
            )
            core = dataclasses.replace(core, blobs=blobs)
        runner = Waking()
        app = create_sync_app(Hosted(core, runner, Redactor()), authenticate or admitting(phone), routers, **options)
        async with client_of(app) as client:
            yield core, client, phone, runner
