package campuslife.android.link

import androidx.test.ext.junit.runners.AndroidJUnit4
import campuslife.android.data.CaptureOutbox.NewFile
import campuslife.android.data.TransferState
import campuslife.android.link.LinkRig.Companion.FIRST
import campuslife.android.link.LinkRig.Companion.FIRST_RECORD
import campuslife.android.link.LinkRig.Companion.SECOND
import campuslife.android.link.LinkRig.Companion.SECOND_EPOCH
import campuslife.android.link.LinkRig.Companion.SECOND_HUB_ID
import campuslife.android.link.LinkRig.Companion.SECOND_RECORD
import campuslife.android.link.LinkRig.Companion.SECOND_TOKEN
import campuslife.android.link.LinkRig.Companion.VERSION
import campuslife.android.sync.FakeHub
import campuslife.android.sync.HubTrouble
import campuslife.android.sync.Pairing
import campuslife.android.sync.RecordedSchedule
import campuslife.android.sync.SyncOutcome
import campuslife.wire.AttachmentRole
import campuslife.wire.CloudPolicy
import campuslife.wire.ErrorCode
import campuslife.wire.ItemData
import campuslife.wire.ItemKind
import campuslife.wire.Origin
import campuslife.wire.OriginType
import java.io.IOException
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DeviceLinkTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val photo = NewFile(role = AttachmentRole.PHOTO, cloudPolicy = CloudPolicy.LOCAL_ONLY, declaredMime = "image/jpeg")
    private lateinit var rig: LinkRig
    private val first get() = rig.first
    private val second get() = rig.second
    private val schedule get() = rig.schedule
    private val db get() = rig.db
    private val captures get() = rig.captures
    private val items get() = rig.items
    private val store get() = rig.store
    private val vault get() = rig.vault
    private val link get() = rig.link

    @Before
    fun open() {
        rig = LinkRig(folder.root)
    }

    @After
    fun close() = rig.close()

    @Test
    fun `before pairing there is nothing to sync or reissue`() {
        assertNull(link.current())
        assertNull(link.linked())
        assertEquals(SyncOutcome.NotPaired, link.sync(false))
        assertEquals(DeviceLink.Reissued.NotPaired, link.reissue())
        assertEquals(emptyList<FakeHub.Asked>(), first.requests)
    }

    @Test
    fun `a first pairing keeps the link and starts the passes`() {
        assertEquals(DeviceLink.Paired.Linked, link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone"))

        assertEquals(FIRST_RECORD, vault.read())
        assertEquals(Linked(FIRST, FakeHub.HUB_ID, FakeHub.DEVICE_ID), link.linked())
        assertEquals(HubLink(FIRST, FakeHub.HUB_ID, FakeHub.TOKEN), link.current())
        assertEquals(RecordedSchedule(syncs = 1, periodic = 1), schedule)
        val sent = first.asked("POST", "/v1/pair").single().json()
        assertEquals(
            listOf(FakeHub.SECRET, "android", "phone", VERSION),
            listOf("secret", "platform", "device_name", "app_version").map { sent.getValue(it).jsonPrimitive.content },
        )
    }

    @Test
    fun `a refused pairing is sent once and keeps the link there was`() {
        link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone")
        first.once({ it.path == "/v1/pair" }) { request -> first.refusal(request, 401, "unauthorized") }

        assertThrows(Pairing.Refused::class.java) { link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone") }

        assertEquals(2, first.asked("POST", "/v1/pair").size)
        assertEquals(FIRST_RECORD, vault.read())
        assertEquals(RecordedSchedule(syncs = 1, periodic = 1), schedule)
    }

    @Test
    fun `an answer from a Hub other than the offer's pairs nothing`() {
        assertThrows(HubTrouble.OtherHub::class.java) { link.pair(SECOND, FakeHub.SECRET, FakeHub.HUB_ID, "phone") }

        assertNull(vault.read())
        assertEquals(RecordedSchedule(), schedule)
    }

    @Test
    fun `pairing again with the same Hub keeps the store as it is and starts the stopped passes again`() {
        link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone")
        items.create(course("자료구조"))
        assertEquals(SyncOutcome.Synced(), link.sync(false))
        val waiting = items.create(course("운영체제"))
        store.stop("device_revoked")
        val standing = store.standing()!!
        val cached = db.cache().rowsOf("item")
        val outbox = everyRow()
        first.token = "fake-token-again"

        assertEquals(DeviceLink.Paired.Linked, link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone"))

        assertEquals(standing.copy(stopped = null), store.standing())
        assertEquals(cached, db.cache().rowsOf("item"))
        assertEquals(outbox, everyRow())
        assertEquals(FIRST_RECORD.copy(token = "fake-token-again"), vault.read())
        assertEquals(RecordedSchedule(syncs = 2, periodic = 2), schedule)
        assertEquals(SyncOutcome.Synced(), link.sync(false))
        assertNotNull(first.row("item", waiting))
        assertEquals(standing.epoch, store.standing()?.epoch)
    }

    @Test
    fun `pairing with another Hub that the user does not confirm leaves the store and the vault as they were`() {
        val before = syncedWithFirst()
        val standing = store.standing()
        val cached = db.cache().rowsOf("item")
        val outbox = everyRow()

        val other = link.pair(SECOND, FakeHub.SECRET, SECOND_HUB_ID, "phone")

        assertTrue(other is DeviceLink.Paired.OtherHub)
        assertEquals(3L, (other as DeviceLink.Paired.OtherHub).unsent)
        assertEquals(FIRST_RECORD, vault.read())
        assertEquals(standing, store.standing())
        assertEquals(cached, db.cache().rowsOf("item"))
        assertEquals(outbox, everyRow())
        assertEquals(RecordedSchedule(syncs = 1, periodic = 1), schedule)
        assertEquals(listOf("POST /v1/pair"), second.requests.map { "${it.method} ${it.path}" })
        assertEquals(SyncOutcome.Synced(), link.sync(false))
        assertNotNull(first.row("capture", before.capture))
    }

    @Test
    fun `a confirmed move to another Hub sends it the unsent and the refused and forgets the previous Hub`() {
        val before = syncedWithFirst()
        val other = link.pair(SECOND, FakeHub.SECRET, SECOND_HUB_ID, "phone") as DeviceLink.Paired.OtherHub

        link.confirm(other)

        assertEquals(SECOND_RECORD, vault.read())
        assertNull(store.standing())
        assertEquals(emptyList<Any>(), db.cache().rowsOf("item"))
        assertEquals(listOf(before.refused, before.unsent, before.capture), everyRow().map { it.entityId })
        assertEquals(listOf(TransferState.PENDING.column), everyRow().map { it.state }.distinct())
        assertEquals(RecordedSchedule(syncs = 2, periodic = 2), schedule)

        assertEquals(SyncOutcome.Synced(), link.sync(false))

        assertEquals(listOf(SECOND_HUB_ID, SECOND_EPOCH), store.standing().let { listOf(it?.hubId, it?.epoch) })
        assertNotNull(second.row("item", before.refused))
        assertNotNull(second.row("item", before.unsent))
        assertNull(second.row("item", before.applied))
        assertNull(db.cache().get("item", before.applied))
        assertNotNull(second.row("capture", before.capture))
        assertEquals(setOf(before.sha256), second.stored.keys)
        val calls = second.requests.map { "${it.method} ${it.path}" }
        assertTrue(calls.indexOf("POST /v1/mutations") < calls.indexOf("POST /v1/blobs/uploads"))
        assertTrue(calls.indexOf("POST /v1/blobs/uploads") < calls.lastIndexOf("POST /v1/mutations"))
        assertEquals(TransferState.SETTLED.column, everyRow().single { it.entityId == before.capture }.state)
        assertEquals(listOf("Bearer $SECOND_TOKEN"), second.requests.drop(1).mapNotNull { it.headers["Authorization"] }.distinct())
    }

    @Test
    fun `a move to another Hub cut short before the store changed is finished by the next pass`() {
        val before = syncedWithFirst()
        vault.write(SECOND_RECORD)

        assertEquals(SyncOutcome.Synced(), link.sync(false))

        assertEquals(SECOND_HUB_ID, store.standing()?.hubId)
        assertNotNull(second.row("capture", before.capture))
        assertNull(db.cache().get("item", before.applied))
    }

    @Test
    fun `a pass whose token was replaced while it ran goes once more with the latest`() {
        link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone")
        first.once({ it.path == "/v1/hub" }) { request ->
            vault.reissued(FakeHub.TOKEN, "fake-token-9")
            first.token = "fake-token-9"
            first.refusal(request, 401, "unauthorized")
        }

        assertEquals(SyncOutcome.Synced(), link.sync(false))

        assertEquals(
            listOf("Bearer ${FakeHub.TOKEN}", "Bearer fake-token-9"),
            first.asked("GET", "/v1/hub").map { it.headers["Authorization"] },
        )
        assertEquals(FIRST_RECORD.copy(token = "fake-token-9"), vault.read())
    }

    @Test
    fun `a refused latest token is unauthorized after one try`() {
        link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone")
        first.token = "fake-token-elsewhere"

        assertEquals(SyncOutcome.Unauthorized, link.sync(false))

        assertEquals(1, first.asked("GET", "/v1/hub").size)
        assertEquals(FIRST_RECORD, vault.read())
    }

    @Test
    fun `a reissued token is used from the next pass and becomes the token once it works`() {
        link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone")

        assertEquals(DeviceLink.Reissued.Done, link.reissue())

        assertEquals("Bearer ${FakeHub.TOKEN}", first.asked("POST", "/v1/devices/me/token").single().headers["Authorization"])
        assertEquals(FIRST_RECORD.copy(pendingToken = "fake-token-1"), vault.read())
        assertEquals(HubLink(FIRST, FakeHub.HUB_ID, "fake-token-1"), link.current())
        assertEquals(RecordedSchedule(syncs = 2, periodic = 2), schedule)
        assertEquals(SyncOutcome.Synced(), link.sync(false))
        assertEquals("Bearer fake-token-1", first.asked("GET", "/v1/hub").single().headers["Authorization"])
        assertEquals(FIRST_RECORD.copy(token = "fake-token-1"), vault.read())
    }

    @Test
    fun `of two reissues before either token is used only the last token is kept`() {
        link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone")

        link.reissue()
        link.reissue()

        assertEquals(
            listOf("Bearer ${FakeHub.TOKEN}", "Bearer fake-token-1"),
            first.asked("POST", "/v1/devices/me/token").map { it.headers["Authorization"] },
        )
        assertEquals(FIRST_RECORD.copy(token = "fake-token-1", pendingToken = "fake-token-2"), vault.read())
        assertEquals(SyncOutcome.Synced(), link.sync(false))
        assertEquals(FIRST_RECORD.copy(token = "fake-token-2"), vault.read())
    }

    @Test
    fun `a refused reissue keeps the link there was`() {
        link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone")
        first.refuseToken = "device_revoked"

        assertEquals(DeviceLink.Reissued.Refused(401, ErrorCode.DEVICE_REVOKED), link.reissue())

        assertEquals(FIRST_RECORD, vault.read())
        assertEquals(RecordedSchedule(syncs = 1, periodic = 1), schedule)
    }

    /** What a device that synced with the first Hub holds: one Item settled, one refused, and unsent ones. */
    private data class Before(val applied: String, val refused: String, val unsent: String, val capture: String, val sha256: String)

    /**
     * The capture's Blob went up to the first Hub, and the connection was cut before its commit went, so the
     * commit waits as pending for a Blob the first Hub has.
     */
    private fun syncedWithFirst(): Before {
        link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone")
        val applied = items.create(course("자료구조"))
        assertEquals(SyncOutcome.Synced(), link.sync(false))
        val refused = items.create(course("거절될 과목"))
        val refusedMutation = db.outbox().overlayOf("item", refused).single().mutationId
        first.once({ it.path == "/v1/mutations" }) { request ->
            first.response(request, 200, """{"results":[{"mutation_id":"$refusedMutation","result":"rejected","reason":"schema"}]}""")
        }
        assertEquals(SyncOutcome.Synced(), link.sync(false))
        val unsent = items.create(course("운영체제"))
        val capture = captures.open(Origin(OriginType.CAMERA))
        captures.addFile(capture, "칠판 사진".toByteArray().inputStream(), photo)
        captures.seal(capture)
        first.once({ it.path == "/v1/mutations" }) { throw IOException("connection cut") }
        assertEquals(SyncOutcome.HubNotReached, link.sync(false))
        val sha256 = first.stored.keys.single()
        assertEquals(TransferState.PENDING.column, everyRow().single { it.entityId == capture }.state)
        return Before(applied, refused, unsent, capture, sha256)
    }

    private fun everyRow() = db.outbox().inStates(TransferState.entries.map { it.column })

    private fun course(title: String) = ItemData(kind = ItemKind.COURSE, title = title, fields = JsonObject(emptyMap()))
}
