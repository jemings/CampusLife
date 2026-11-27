import asyncio
import hashlib
import logging
from datetime import timedelta
from functools import partial

import pytest
from campuslife_contracts import validate
from conftest import contract_fixture
from device_bench import PUBLIC_URL, START, UNKNOWN_SECRET, bench, request
from test_capture_commit import commit, feed

from campuslife_hub import audit, backup, devices, mutations
from campuslife_hub.audit import AuditEvent
from campuslife_hub.blobs import NoUploadSession
from campuslife_hub.redaction import Redactor
from campuslife_hub.devices import (
    ClientPlatform,
    DeviceState,
    HubDeviceRefused,
    NoPublicUrl,
    Refusal,
    RefusalCode,
    RevokedDevice,
    UnknownDevice,
    ensure_hub_device,
)


async def test_the_hub_has_one_device_row_of_its_own(db):
    first = await db.write(ensure_hub_device)
    second = await db.write(ensure_hub_device)
    assert first == second
    assert validate("common/uuid", first) == []


async def test_an_offer_is_a_pairing_offer_good_for_five_minutes(tmp_path):
    async with bench(tmp_path) as b:
        offer = await b.offer()
        assert validate("device/pairing_offer", offer.wire()) == []
        assert (offer.hub_url, offer.hub_id) == (PUBLIC_URL, b.core.identity.hub_id)
        assert offer.expires_at == START + timedelta(minutes=5)


async def test_without_a_public_url_there_is_no_offer(tmp_path):
    """#19 decision 4: the add-device screen shows how to set serve --public-url instead."""
    async with bench(tmp_path) as b:
        with pytest.raises(NoPublicUrl):
            await devices.offer(b.core, now=b.clock(), public_url=None, redactor=b.redactor)


async def test_pairing_with_an_offered_secret_makes_an_active_device_its_token_names(tmp_path):
    async with bench(tmp_path) as b:
        paired = await b.paired(platform=ClientPlatform.IPADOS, name="iPad Air", version="0.2.0")

        assert validate("device/pair_response", paired.wire()) == []
        assert await b.authenticate(paired.token) == paired.device_id
        record = await b.device(paired.device_id)
        assert (record.platform, record.device_name, record.app_version, record.state) == ("ipados", "iPad Air", "0.2.0", DeviceState.ACTIVE)
        assert record.created_at == "2026-03-02T09:00:00.000Z"


async def test_each_pairing_makes_another_device_with_its_own_token(tmp_path):
    async with bench(tmp_path) as b:
        phone, tablet = await b.paired(), await b.paired(platform=ClientPlatform.IPADOS)
        assert phone.device_id != tablet.device_id and phone.token != tablet.token
        assert (await b.authenticate(phone.token), await b.authenticate(tablet.token)) == (phone.device_id, tablet.device_id)


UNAUTHORIZED = Refusal(RefusalCode.UNAUTHORIZED)


@pytest.mark.parametrize(
    "bearer",
    [
        pytest.param(None, id="no Authorization: Bearer"),
        pytest.param("not a token", id="characters a token never holds"),
        pytest.param("\ud800", id="text that is not even UTF-8"),
        pytest.param("A" * 513, id="longer than a token is"),
        pytest.param("A" * 43, id="a token the Hub never issued"),
    ],
)
async def test_a_bearer_that_names_no_token_is_unauthorized(tmp_path, bearer):
    async with bench(tmp_path) as b:
        await b.paired()
        assert await b.authenticate(bearer) == UNAUTHORIZED


DORMANT = Refusal(RefusalCode.DEVICE_DORMANT)


@pytest.mark.parametrize(
    ("idle", "answer"),
    [
        pytest.param(timedelta(days=90), "device", id="exactly the dormant period"),
        pytest.param(timedelta(days=90, milliseconds=1), DORMANT, id="longer than the dormant period"),
    ],
)
async def test_a_device_unseen_for_longer_than_90_days_is_dormant(tmp_path, idle, answer):
    async with bench(tmp_path) as b:
        paired = await b.paired()
        b.clock.advance(seconds=idle.total_seconds())
        assert await b.authenticate(paired.token) == (paired.device_id if answer == "device" else answer)


async def test_a_device_that_turns_dormant_stays_dormant_and_the_audit_log_says_why(tmp_path):
    async with bench(tmp_path) as b:
        paired = await b.paired()
        b.clock.advance(days=91)
        await b.authenticate(paired.token)
        b.dormant_after = timedelta(days=365)

        assert await b.authenticate(paired.token) == DORMANT
        assert (await b.device(paired.device_id)).state == DeviceState.DORMANT
        dormant = [entry for entry in await b.core.db.read(audit.entries) if entry.event == AuditEvent.DORMANT]
        assert [(entry.at, entry.subject, entry.details) for entry in dormant] == [("2026-06-01T09:00:00.000Z", paired.device_id, {"reason": "inactive"})]


async def test_every_authenticated_request_starts_the_dormant_period_over(tmp_path):
    async with bench(tmp_path) as b:
        paired = await b.paired()
        b.clock.advance(days=89)
        assert await b.authenticate(paired.token) == paired.device_id
        b.clock.advance(days=89)
        assert await b.authenticate(paired.token) == paired.device_id


async def test_the_dormant_period_is_a_setting(tmp_path):
    async with bench(tmp_path, dormant_after=timedelta(days=7)) as b:
        paired = await b.paired()
        b.clock.advance(days=7, seconds=1)
        assert await b.authenticate(paired.token) == DORMANT


async def test_last_seen_at_is_written_only_once_it_is_more_than_an_hour_old(tmp_path):
    async with bench(tmp_path) as b:
        paired = await b.paired()
        assert (await b.device(paired.device_id)).last_seen_at is None
        seen = []
        for minutes in (0, 60, 0.001, 30):
            b.clock.advance(minutes=minutes)
            assert await b.authenticate(paired.token) == paired.device_id
            seen.append((await b.device(paired.device_id)).last_seen_at)
        assert seen == ["2026-03-02T09:00:00.000Z", "2026-03-02T09:00:00.000Z", "2026-03-02T10:00:00.060Z", "2026-03-02T10:00:00.060Z"]


async def test_last_rejected_at_is_written_only_once_it_is_more_than_an_hour_old(tmp_path):
    async with bench(tmp_path) as b:
        paired = await b.paired()
        b.clock.advance(days=91)
        rejected = []
        for minutes in (0, 60, 0.001):
            b.clock.advance(minutes=minutes)
            assert await b.authenticate(paired.token) == DORMANT
            rejected.append((await b.device(paired.device_id)).last_rejected_at)
        assert rejected == ["2026-06-01T09:00:00.000Z", "2026-06-01T09:00:00.000Z", "2026-06-01T10:00:00.060Z"]


REVOKED = Refusal(RefusalCode.DEVICE_REVOKED)


async def test_a_revoked_device_is_refused_and_no_other_device_is(tmp_path):
    async with bench(tmp_path) as b:
        lost, kept = await b.paired(name="잃어버린 휴대폰"), await b.paired(platform=ClientPlatform.IPADOS)

        assert await b.revoke(lost.device_id) is True

        assert await b.authenticate(lost.token) == REVOKED
        assert await b.authenticate(kept.token) == kept.device_id
        assert [(entry.event, entry.subject, entry.details) for entry in await b.core.db.read(audit.entries) if entry.event == AuditEvent.REVOKE] == [
            (AuditEvent.REVOKE, lost.device_id, {"reason": "user"})
        ]


async def test_a_revoke_is_for_good(tmp_path):
    """Neither the dormant period nor an approval brings a revoked Device back (D6)."""
    async with bench(tmp_path) as b:
        lost = await b.paired()
        await b.revoke(lost.device_id)
        b.clock.advance(days=200)

        assert await b.authenticate(lost.token) == REVOKED
        with pytest.raises(RevokedDevice):
            await b.approve(lost.device_id)
        assert (await b.device(lost.device_id)).state == DeviceState.REVOKED


async def test_revoking_a_dormant_device_revokes_it(tmp_path):
    async with bench(tmp_path) as b:
        lost = await b.paired()
        b.clock.advance(days=91)
        await b.authenticate(lost.token)

        assert await b.revoke(lost.device_id) is True
        assert await b.authenticate(lost.token) == REVOKED


async def test_revoking_a_revoked_device_again_changes_nothing(tmp_path):
    async with bench(tmp_path) as b:
        lost = await b.paired()
        await b.revoke(lost.device_id)
        b.clock.advance(hours=1)

        assert await b.revoke(lost.device_id) is False
        assert len([entry for entry in await b.core.db.read(audit.entries) if entry.event == AuditEvent.REVOKE]) == 1


async def test_revoking_a_device_ends_its_uploads_and_leaves_other_devices_uploads_alone(tmp_path):
    async with bench(tmp_path) as b:
        lost, kept = await b.paired(), await b.paired(platform=ClientPlatform.IPADOS)
        data = b"half-sent lecture"
        sha = hashlib.sha256(data).hexdigest()
        for device in (lost, kept):
            await b.core.blobs.open_upload(device.device_id, sha, len(data))

        await b.revoke(lost.device_id)

        with pytest.raises(NoUploadSession):
            await b.core.blobs.put_chunk(lost.device_id, sha, 0, data)
        await b.core.blobs.put_chunk(kept.device_id, sha, 0, data)
        assert (await b.core.blobs.open_upload(kept.device_id, sha, len(data))).received == (0,)


async def test_revoking_a_device_keeps_what_it_synced(tmp_path):
    async with bench(tmp_path) as b:
        lost = await b.paired()
        capture = contract_fixture("valid", "capture/commit", "text_only")
        assert (await mutations.apply(b.core.db, [commit(capture)], lost.device_id))[0]["result"] == "applied"
        before = await feed(b.core.db)

        await b.revoke(lost.device_id)

        assert await feed(b.core.db) == before


async def test_a_device_paired_again_after_a_revoke_sends_its_unacknowledged_capture_without_making_another(tmp_path):
    """#19 decision 7: the Outbox of a revoked Device goes up again under its new device_id; capture.commit compares
    the content without device_id, so the Capture the Hub already holds answers applied and nothing is written."""
    async with bench(tmp_path) as b:
        lost = await b.paired()
        capture = contract_fixture("valid", "capture/commit", "text_only")
        await mutations.apply(b.core.db, [commit(capture)], lost.device_id)
        before = await feed(b.core.db)
        await b.revoke(lost.device_id)

        again = await b.paired()
        resent = commit(capture)

        assert again.device_id != lost.device_id
        assert await mutations.apply(b.core.db, [resent], again.device_id) == [{"mutation_id": resent["mutation_id"], "result": "applied"}]
        assert await feed(b.core.db) == before


@pytest.mark.parametrize("operation", ["revoke", "approve"])
async def test_the_hubs_own_device_is_never_revoked_or_approved(tmp_path, operation):
    async with bench(tmp_path) as b:
        with pytest.raises(HubDeviceRefused):
            await getattr(b, operation)(b.core.hub_device)
        assert (await b.device(b.core.hub_device)).state == DeviceState.ACTIVE


@pytest.mark.parametrize("operation", ["revoke", "approve"])
async def test_a_device_the_hub_does_not_know_is_neither_revoked_nor_approved(tmp_path, operation):
    async with bench(tmp_path) as b:
        with pytest.raises(UnknownDevice):
            await getattr(b, operation)("6ba7b811-9dad-41d1-80b4-00c04fd430c8")


async def test_approving_a_dormant_device_lets_the_same_token_in_again_and_starts_its_dormant_period_over(tmp_path):
    """#19 decision 6: without the fresh start the Device, unseen for 200 days, would turn dormant on its next request."""
    async with bench(tmp_path) as b:
        paired = await b.paired()
        b.clock.advance(days=200)
        assert await b.authenticate(paired.token) == DORMANT

        assert await b.approve(paired.device_id) is True

        assert await b.authenticate(paired.token) == paired.device_id
        b.clock.advance(days=90)
        assert await b.authenticate(paired.token) == paired.device_id
        approvals = [(entry.at, entry.subject) for entry in await b.core.db.read(audit.entries) if entry.event == AuditEvent.APPROVE]
        assert approvals == [("2026-09-18T09:00:00.000Z", paired.device_id)]


async def test_approving_an_active_device_changes_nothing(tmp_path):
    async with bench(tmp_path) as b:
        paired = await b.paired()
        assert await b.approve(paired.device_id) is False
        assert [entry for entry in await b.core.db.read(audit.entries) if entry.event == AuditEvent.APPROVE] == []


async def test_a_device_restored_dormant_from_a_100_day_old_backup_syncs_on_with_the_same_id_once_approved(tmp_path):
    async with bench(tmp_path) as b:
        phone = await b.paired()
        capture = contract_fixture("valid", "capture/commit", "text_only")
        await mutations.apply(b.core.db, [commit(capture)], phone.device_id)
        await backup.back_up(b.core, tmp_path / "drive", clock=b.clock)
        before = await feed(b.core.db)
    clock = b.clock
    clock.advance(days=100)
    await backup.restore(tmp_path / "drive", tmp_path / "restored", sync_roots=[])

    async with bench(tmp_path, root="restored", clock=clock) as b:
        assert await b.authenticate(phone.token) == DORMANT
        assert await b.approve(phone.device_id) is True

        assert await b.authenticate(phone.token) == phone.device_id
        assert await feed(b.core.db) == before
        again = commit(capture)
        assert await mutations.apply(b.core.db, [again], phone.device_id) == [{"mutation_id": again["mutation_id"], "result": "applied"}]


async def test_a_restore_records_each_device_it_made_dormant(tmp_path):
    async with bench(tmp_path) as b:
        active, idle, lost = await b.paired(), await b.paired(), await b.paired()
        b.clock.advance(days=91)
        await b.authenticate(active.token)
        await b.authenticate(idle.token)
        b.clock.advance(days=89)
        await b.approve(active.device_id)
        await b.revoke(lost.device_id)
        await backup.back_up(b.core, tmp_path / "drive", clock=b.clock)
        seen = len(await b.core.db.read(audit.entries))

    await backup.restore(tmp_path / "drive", tmp_path / "restored", sync_roots=[])

    async with bench(tmp_path, root="restored") as b:
        restored = await b.core.db.read(lambda snap: audit.entries(snap, after=seen))
    assert [(entry.event, entry.subject, entry.details) for entry in restored] == [(AuditEvent.DORMANT, active.device_id, {"reason": "restore"})]


async def test_marking_dormant_turns_every_device_unseen_past_the_period_dormant_and_records_each(tmp_path):
    async with bench(tmp_path) as b:
        idle, busy, lost = await b.paired(), await b.paired(), await b.paired()
        await b.revoke(lost.device_id)
        b.clock.advance(days=60)
        await b.authenticate(busy.token)
        b.clock.advance(days=31)

        assert await devices.mark_dormant(b.core, now=b.clock(), dormant_after=timedelta(days=90)) == [idle.device_id]

        assert [(await b.device(paired.device_id)).state for paired in (idle, busy, lost)] == [DeviceState.DORMANT, DeviceState.ACTIVE, DeviceState.REVOKED]
        dormant = [(entry.subject, entry.details) for entry in await b.core.db.read(audit.entries) if entry.event == AuditEvent.DORMANT]
        assert dormant == [(idle.device_id, {"reason": "inactive"})]
        assert await devices.mark_dormant(b.core, now=b.clock(), dormant_after=timedelta(days=90)) == []


async def test_marking_dormant_leaves_the_hubs_own_device_active(tmp_path):
    async with bench(tmp_path) as b:
        far_future = b.clock.advance(days=3650)
        assert await devices.mark_dormant(b.core, now=far_future, dormant_after=timedelta(days=90)) == []
        assert (await b.device(b.core.hub_device)).state == DeviceState.ACTIVE


async def pair_failures(b):
    return [entry.details for entry in await b.core.db.read(audit.entries) if entry.event == AuditEvent.PAIR_FAILED]


async def test_a_secret_the_hub_never_offered_pairs_nothing(tmp_path):
    async with bench(tmp_path) as b:
        await b.offer()
        assert await b.pair(UNKNOWN_SECRET) == UNAUTHORIZED
        assert await pair_failures(b) == [{"reason": "unknown_secret"}]


@pytest.mark.parametrize(("after", "paired"), [(timedelta(minutes=5) - timedelta(milliseconds=1), True), (timedelta(minutes=5), False)])
async def test_an_offer_is_good_for_five_minutes(tmp_path, after, paired):
    async with bench(tmp_path) as b:
        offer = await b.offer()
        b.clock.advance(seconds=after.total_seconds())
        answer = await b.pair(offer.secret)
        assert isinstance(answer, devices.Paired) is paired
        assert await pair_failures(b) == ([] if paired else [{"reason": "expired_secret"}])


async def test_a_new_offer_ends_the_one_before_it(tmp_path):
    async with bench(tmp_path) as b:
        first = await b.offer()
        second = await b.offer()
        assert await b.pair(first.secret) == UNAUTHORIZED
        assert isinstance(await b.pair(second.secret), devices.Paired)


async def test_a_secret_used_twice_is_refused_and_the_device_it_paired_is_revoked(tmp_path):
    """D6: the second use means someone else holds the secret, so neither holder is trusted."""
    async with bench(tmp_path) as b:
        offer = await b.offer()
        first = await b.pair(offer.secret)
        sha = hashlib.sha256(b"first upload").hexdigest()
        await b.core.blobs.open_upload(first.device_id, sha, 12)

        assert await b.pair(offer.secret, name="someone else") == UNAUTHORIZED

        assert await b.authenticate(first.token) == REVOKED
        with pytest.raises(NoUploadSession):
            await b.core.blobs.put_chunk(first.device_id, sha, 0, b"first upload")
        assert await pair_failures(b) == [{"reason": "secret_reused"}]
        revokes = [(entry.subject, entry.details) for entry in await b.core.db.read(audit.entries) if entry.event == AuditEvent.REVOKE]
        assert revokes == [(first.device_id, {"reason": "pairing_secret_reused"})]


async def test_a_used_secret_is_caught_after_the_hub_restarts(tmp_path):
    async with bench(tmp_path) as b:
        offer = await b.offer()
        first = await b.pair(offer.secret)
    async with bench(tmp_path) as b:
        assert await b.pair(offer.secret) == UNAUTHORIZED
        assert await b.authenticate(first.token) == REVOKED


async def test_the_hubs_own_platform_never_pairs(tmp_path):
    async with bench(tmp_path) as b:
        offer = await b.offer()
        with pytest.raises(ValueError):
            await b.pair(offer.secret, platform="windows_hub")
        assert isinstance(await b.pair(offer.secret), devices.Paired)


async def test_after_ten_failed_pairings_in_ten_minutes_no_secret_is_judged_until_the_first_is_ten_minutes_old(tmp_path):
    async with bench(tmp_path) as b:
        for _ in range(10):
            assert await b.pair(UNKNOWN_SECRET) == UNAUTHORIZED
        b.clock.advance(minutes=6)
        offer = await b.offer()

        assert await b.pair(offer.secret) == Refusal(RefusalCode.UNAUTHORIZED, retry_after=240)
        b.clock.advance(seconds=1.5)
        assert await b.pair(offer.secret) == Refusal(RefusalCode.UNAUTHORIZED, retry_after=239)
        b.clock.advance(seconds=238.5, milliseconds=-1)
        assert await b.pair(offer.secret) == Refusal(RefusalCode.UNAUTHORIZED, retry_after=1)
        b.clock.advance(milliseconds=1)

        paired = await b.pair(offer.secret)
        assert await b.authenticate(paired.token) == paired.device_id
        assert len(await pair_failures(b)) == 10


async def test_pairing_attempts_that_arrive_together_are_judged_one_at_a_time_so_the_limit_holds(tmp_path):
    async with bench(tmp_path) as b:
        answers = await asyncio.gather(*(b.pair(UNKNOWN_SECRET) for _ in range(50)))

        assert len(await pair_failures(b)) == 10
        assert sorted(answer.retry_after is None for answer in answers) == [False] * 40 + [True] * 10


async def test_failed_pairings_spread_over_more_than_ten_minutes_are_not_limited(tmp_path):
    async with bench(tmp_path) as b:
        for _ in range(30):
            assert await b.pair(UNKNOWN_SECRET) == UNAUTHORIZED
            b.clock.advance(minutes=1, seconds=1)


async def test_successful_pairings_are_never_limited(tmp_path):
    async with bench(tmp_path) as b:
        for _ in range(9):
            await b.pair(UNKNOWN_SECRET)
        for _ in range(5):
            assert isinstance(await b.paired(), devices.Paired)


async def test_the_failures_are_forgotten_when_the_hub_restarts(tmp_path):
    async with bench(tmp_path) as b:
        for _ in range(10):
            await b.pair(UNKNOWN_SECRET)
    async with bench(tmp_path, clock=b.clock) as b:
        assert isinstance(await b.paired(), devices.Paired)


async def test_the_audit_log_keeps_the_tailscale_login_a_pairing_came_with(tmp_path):
    async with bench(tmp_path) as b:
        offer = await b.offer()
        attempt = partial(devices.pair, b.core, now=b.clock(), limiter=b.limiter, redactor=b.redactor, tailscale_user="student@example.com")
        paired = await attempt(request(offer.secret))
        await attempt(request(UNKNOWN_SECRET))
        await b.pair(UNKNOWN_SECRET)

        logged = [(entry.event, entry.subject, entry.details) for entry in await b.core.db.read(audit.entries)]
        assert logged == [
            (AuditEvent.PAIR, paired.device_id, {"platform": "android", "tailscale_user": "student@example.com"}),
            (AuditEvent.PAIR_FAILED, None, {"reason": "unknown_secret", "tailscale_user": "student@example.com"}),
            (AuditEvent.PAIR_FAILED, None, {"reason": "unknown_secret"}),
        ]


async def test_a_reissued_token_leaves_the_old_one_working_until_its_own_first_use_retires_it(tmp_path):
    async with bench(tmp_path) as b:
        paired = await b.paired()
        b.clock.advance(minutes=1)
        reissued = await b.reissue(paired.device_id)

        assert validate("device/pair_response", reissued.wire()) == []
        assert reissued.device_id == paired.device_id and reissued.token != paired.token
        assert await b.authenticate(paired.token) == paired.device_id
        b.clock.advance(minutes=1)
        assert await b.authenticate(reissued.token) == paired.device_id
        assert await b.authenticate(paired.token) == UNAUTHORIZED
        assert await b.authenticate(reissued.token) == paired.device_id
        logged = [(entry.at, entry.event, entry.subject) for entry in await b.core.db.read(audit.entries) if entry.event != AuditEvent.PAIR]
        assert logged == [("2026-03-02T09:01:00.000Z", AuditEvent.REISSUE, paired.device_id), ("2026-03-02T09:02:00.000Z", AuditEvent.TOKEN_RETIRED, paired.device_id)]


async def test_asking_again_for_a_token_issues_another_and_ends_the_unused_one(tmp_path):
    """#19 decision 2: the Hub keeps only SHA-256 of a token, so it cannot hand the unused one out again."""
    async with bench(tmp_path) as b:
        paired = await b.paired()
        first = await b.reissue(paired.device_id)
        second = await b.reissue(paired.device_id)

        assert len({paired.token, first.token, second.token}) == 3
        assert await b.authenticate(first.token) == UNAUTHORIZED
        assert await b.authenticate(paired.token) == paired.device_id
        assert await b.authenticate(second.token) == paired.device_id
        assert await b.authenticate(paired.token) == UNAUTHORIZED


async def test_a_token_reissued_after_a_reissue_was_used_retires_the_one_before(tmp_path):
    async with bench(tmp_path) as b:
        paired = await b.paired()
        first = await b.reissue(paired.device_id)
        await b.authenticate(first.token)
        second = await b.reissue(paired.device_id)

        assert await b.authenticate(first.token) == paired.device_id
        assert await b.authenticate(second.token) == paired.device_id
        assert await b.authenticate(first.token) == UNAUTHORIZED


async def test_a_reissued_token_used_while_its_device_is_dormant_retires_nothing(tmp_path):
    async with bench(tmp_path) as b:
        paired = await b.paired()
        reissued = await b.reissue(paired.device_id)
        b.clock.advance(days=91)

        assert await b.authenticate(reissued.token) == DORMANT
        await b.approve(paired.device_id)
        assert await b.authenticate(paired.token) == paired.device_id


async def test_every_token_of_a_revoked_device_answers_device_revoked(tmp_path):
    async with bench(tmp_path) as b:
        paired = await b.paired()
        reissued = await b.reissue(paired.device_id)
        await b.revoke(paired.device_id)

        assert (await b.authenticate(paired.token), await b.authenticate(reissued.token)) == (REVOKED, REVOKED)


async def test_a_heartbeat_keeps_the_last_status_a_device_reported_and_its_app_version(tmp_path):
    async with bench(tmp_path) as b:
        paired = await b.paired(version="0.1.0")
        full, minimal = contract_fixture("valid", "device/status", "full"), contract_fixture("valid", "device/status", "minimal")
        full["app_version"], minimal["app_version"] = "0.1.1", "0.2.0"

        await devices.heartbeat(b.core, paired.device_id, full, now=b.clock())
        b.clock.advance(minutes=15)
        await devices.heartbeat(b.core, paired.device_id, minimal, now=b.clock())

        record = await b.device(paired.device_id)
        assert (record.last_status, record.last_status_at, record.app_version) == (minimal, "2026-03-02T09:15:00.000Z", "0.2.0")


def database_bytes(core) -> bytes:
    return b"".join(path.read_bytes() for path in core.layout.database.parent.iterdir() if path.name.startswith(core.layout.database.name))


async def test_no_token_or_secret_is_kept_in_the_database_or_its_wal(tmp_path):
    async with bench(tmp_path) as b:
        used = await b.offer()
        paired = await b.pair(used.secret)
        reissued = await b.reissue(paired.device_id)
        await b.authenticate(reissued.token)
        open_offer = await b.offer()
        await b.pair(used.secret)
        texts = [used.secret, open_offer.secret, paired.token, reissued.token]

        assert core_holds_none(database_bytes(b.core), texts)
        assert (b.core.layout.database.parent / (b.core.layout.database.name + "-wal")).stat().st_size > 0
    assert core_holds_none(database_bytes(b.core), texts)


def core_holds_none(data: bytes, texts: list[str]) -> bool:
    return not [text for text in texts if text.encode() in data or text.encode("utf-16-le") in data]


async def test_issued_secrets_and_tokens_are_masked_in_log_lines_and_received_ones_once_they_authenticate(tmp_path):
    async with bench(tmp_path) as b:
        offer = await b.offer()
        paired = await b.pair(offer.secret)
        reissued = await b.reissue(paired.device_id)
        for issued in (offer.secret, paired.token, reissued.token):
            assert issued not in b.redactor.redact(f"sent {issued}")
    restarted = Redactor()
    async with bench(tmp_path, redactor=restarted) as b:
        never_issued = "A" * 43
        assert await b.authenticate(never_issued) == UNAUTHORIZED
        assert await b.authenticate(paired.token) == paired.device_id

        assert never_issued in restarted.redact(f"sent {never_issued}")
        assert paired.token not in restarted.redact(f"sent {paired.token}")
        assert reissued.token in restarted.redact(f"sent {reissued.token}")


async def test_no_device_operation_logs_a_token_or_a_secret(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with bench(tmp_path) as b:
        offer = await b.offer()
        paired = await b.pair(offer.secret)
        await b.pair(offer.secret)
        reissued = await b.reissue(paired.device_id)
        for token in (paired.token, reissued.token, None, "A" * 43):
            await b.authenticate(token)
        await b.revoke(paired.device_id)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert [text for text in (offer.secret, paired.token, reissued.token) if text in logged] == []
