package campuslife.android.sync

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import campuslife.android.data.BlobFiles
import campuslife.android.data.CampusLifeDatabase
import campuslife.android.data.CaptureOutbox
import campuslife.android.data.CaptureOutbox.CaptureTransfer
import campuslife.android.data.CaptureOutbox.NewFile
import campuslife.android.data.OutboxRow
import campuslife.android.data.SyncStore
import campuslife.android.data.TransferState
import campuslife.android.data.UserData
import campuslife.wire.AttachmentRole
import campuslife.wire.CloudPolicy
import campuslife.wire.ItemData
import campuslife.wire.ItemKind
import campuslife.wire.Origin
import campuslife.wire.OriginType
import java.io.File
import java.time.Clock
import java.time.Instant
import java.time.ZoneOffset
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeFalse
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class SyncEngineTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val clock = Clock.fixed(Instant.parse("2026-03-02T01:00:00.123Z"), ZoneOffset.UTC)
    private val photo = NewFile(role = AttachmentRole.PHOTO, cloudPolicy = CloudPolicy.LOCAL_ONLY, declaredMime = "image/jpeg")
    private val hub = FakeHub()
    private lateinit var blobDir: File
    private lateinit var db: CampusLifeDatabase
    private lateinit var captures: CaptureOutbox
    private lateinit var items: UserData
    private lateinit var store: SyncStore

    @Before
    fun open() {
        blobDir = folder.newFolder("blobs")
        db = Room.inMemoryDatabaseBuilder(context, CampusLifeDatabase::class.java).allowMainThreadQueries().build()
        val files = BlobFiles(blobDir)
        captures = CaptureOutbox(db, files, clock)
        items = UserData(db)
        store = SyncStore(db, files, clock)
    }

    @After
    fun close() = db.close()

    @Test
    fun `a first pass takes the base epoch from GET hub and sends it with the changes and the Mutations`() {
        items.create(course("자료구조"))

        assertEquals(SyncOutcome.Synced(), engine().run(false))
        engine().run(false)

        assertEquals(
            listOf(
                "GET /v1/hub",
                "POST /v1/mutations",
                "GET /v1/changes",
                "POST /v1/devices/me/status",
                "GET /v1/changes",
                "POST /v1/devices/me/status",
            ),
            hub.requests.map { "${it.method} ${it.path}" },
        )
        for (asked in hub.requests) {
            assertEquals(asked.path, "1", asked.headers["CampusLife-Contract"])
            assertEquals(asked.path, "Bearer ${FakeHub.TOKEN}", asked.headers["Authorization"])
            val epoch = if (asked.path in setOf("/v1/changes", "/v1/mutations")) FakeHub.EPOCH else null
            assertEquals(asked.path, epoch, asked.headers["CampusLife-Epoch"])
        }
    }

    @Test
    fun `a sealed capture goes up in chunks and its commit comes back with the pull`() {
        val id = sealedCapture(ABC, NIST_448)

        assertEquals(SyncOutcome.Synced(), engine().run(false))

        assertEquals(ABC, hub.stored[ABC_SHA]?.decodeToString())
        assertEquals(NIST_448, hub.stored[NIST_448_SHA]?.decodeToString())
        assertEquals((0L..13L).toList(), chunksSent(NIST_448_SHA))
        assertEquals(CaptureTransfer.COMMITTED, transferOf(id))
        assertEquals(TransferState.SETTLED.column, commitRow(id).state)
        assertNotNull(db.cache().get("capture", id))
        assertEquals(2, db.cache().rowsOf("attachment").size)
        assertEquals(emptyList<SyncStore.Upload>(), store.uploads())
    }

    @Test
    fun `an upload cut off midway goes on from the chunks the Hub has`() {
        val id = sealedCapture(NIST_448)
        hub.once({ it.method == "PUT" && it.path.endsWith("/chunks/5") }, FakeHub.cut)

        assertEquals(SyncOutcome.HubNotReached, engine().run(false))
        assertEquals(TransferState.UPLOADING, store.uploads().single().state)
        hub.requests.clear()
        assertEquals(SyncOutcome.Synced(), engine().run(false))

        assertEquals((5L..13L).toList(), chunksSent(NIST_448_SHA))
        assertEquals(NIST_448, hub.stored[NIST_448_SHA]?.decodeToString())
        assertEquals(CaptureTransfer.COMMITTED, transferOf(id))
    }

    @Test
    fun `an upload session the Hub dropped is opened again`() {
        val id = sealedCapture(ABC)
        hub.once({ it.method == "PUT" }, { hub.refusal(it, 404, "not_found") })

        assertEquals(SyncOutcome.Synced(), engine().run(false))

        assertEquals(2, hub.asked("POST", "/v1/blobs/uploads").size)
        assertEquals(CaptureTransfer.COMMITTED, transferOf(id))
    }

    @Test
    fun `a full Hub holds the uploads back and the Outbox still goes`() {
        hub.storageFull = true
        val id = sealedCapture(ABC)
        val course = items.create(course("자료구조"))

        assertEquals(SyncOutcome.Synced(deferred = true), engine().run(false))

        assertEquals(TransferState.RETRY_WAIT, store.uploads().single().state)
        assertEquals(CaptureTransfer.RETRY_WAIT, transferOf(id))
        assertNotNull(hub.row("item", course))
        assertEquals("hub_storage_full", hub.statuses.single()["last_error"]?.jsonPrimitive?.content)
        hub.storageFull = false
        assertEquals(SyncOutcome.Synced(), engine().run(false))
        assertEquals(CaptureTransfer.COMMITTED, transferOf(id))
    }

    @Test
    fun `a copy gone before its upload counts once the Hub has the bytes from another device`() {
        val id = sealedCapture(ABC)
        hub.stored[ABC_SHA] = ABC.toByteArray()
        assertTrue(File(blobDir, ABC_SHA).delete())

        assertEquals(SyncOutcome.Synced(), engine().run(false))

        assertEquals(listOf("POST /v1/blobs/uploads/$ABC_SHA/finalize"), blobRequests())
        assertEquals(CaptureTransfer.COMMITTED, transferOf(id))
    }

    @Test
    fun `a copy gone that the Hub does not have leaves its capture needing attention`() {
        val id = sealedCapture(ABC)
        assertTrue(File(blobDir, ABC_SHA).delete())

        assertEquals(SyncOutcome.Synced(), engine().run(false))

        assertEquals(listOf("POST /v1/blobs/uploads/$ABC_SHA/finalize"), blobRequests())
        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(id))
        assertEquals(emptyList<SyncStore.Upload>(), store.uploads())
    }

    @Test
    fun `a hash_mismatch over a copy that still holds its bytes sends them again`() {
        val id = sealedCapture(ABC)
        hub.once({ it.path.endsWith("/finalize") }, { hub.refusal(it, 409, "hash_mismatch") })

        assertEquals(SyncOutcome.Synced(), engine().run(false))

        assertEquals(ABC, File(blobDir, ABC_SHA).readText())
        assertEquals(2, hub.asked("POST", "/v1/blobs/uploads").size)
        assertEquals(CaptureTransfer.COMMITTED, transferOf(id))
    }

    @Test
    fun `a copy that cannot be read for now waits and stays`() {
        val id = sealedCapture(ABC)
        val copy = File(blobDir, ABC_SHA)
        assertTrue(copy.setReadable(false))
        assumeFalse("this user reads files whatever their mode", copy.canRead())

        assertEquals(SyncOutcome.Synced(deferred = true), engine().run(false))

        assertEquals(TransferState.RETRY_WAIT, store.uploads().single().state)
        assertTrue(copy.exists())
        assertTrue(copy.setReadable(true))
        assertEquals(SyncOutcome.Synced(), engine().run(false))
        assertEquals(CaptureTransfer.COMMITTED, transferOf(id))
    }

    @Test
    fun `a Blob over the large size waits for an unmetered network`() {
        val id = sealedCapture(NIST_448)

        assertEquals(SyncOutcome.Synced(largeWaiting = true), engine(largeBlobBytes = 55).run(false))
        assertEquals(emptyList<FakeHub.Asked>(), hub.asked("POST", "/v1/blobs/uploads"))
        assertEquals(CaptureTransfer.PENDING, transferOf(id))

        assertEquals(SyncOutcome.Synced(), engine(largeBlobBytes = 55).run(true))
        assertEquals(CaptureTransfer.COMMITTED, transferOf(id))
    }

    @Test
    fun `a Blob of exactly the large size goes over any network`() {
        val id = sealedCapture(NIST_448)

        assertEquals(SyncOutcome.Synced(), engine(largeBlobBytes = 56).run(false))

        assertEquals(CaptureTransfer.COMMITTED, transferOf(id))
    }

    @Test
    fun `a batch the Hub refuses for one Mutation sets that one aside and the rest goes`() {
        val first = items.create(course("A"))
        val unreadable = UUID.randomUUID().toString()
        val unreadableItem = UUID.randomUUID().toString()
        db.outbox().add(
            OutboxRow(
                mutationId = unreadable,
                op = "create",
                entity = "item",
                entityId = unreadableItem,
                body = """{"mutation_id":"$unreadable","op":"create","entity":"item","id":"$unreadableItem",""" +
                    """"data":{"kind":"course","title":"B","fields":{"credits":1E+400}}}""",
                state = TransferState.PENDING.column,
            ),
        )
        val last = items.create(course("C"))

        assertEquals(SyncOutcome.Synced(), engine().run(false))

        assertNotNull(hub.row("item", first))
        assertNotNull(hub.row("item", last))
        assertNull(hub.row("item", unreadableItem))
        assertEquals(TransferState.REJECTED.column, db.outbox().byMutationId(unreadable)?.state)
        assertEquals(1L, hub.statuses.single()["rejected_mutation_count"]?.jsonPrimitive?.long)
    }

    @Test
    fun `a Hub restored from a backup gets every answered Mutation again and the feed from 0`() {
        val kept = items.create(course("A"))
        engine().run(false)
        val backup = hub.backup()
        val lost = items.create(course("B"))
        engine().run(false)
        val cursor = store.standing()?.cursor
        hub.restore(backup, EPOCH_2)
        hub.requests.clear()

        assertEquals(SyncOutcome.Synced(), engine().run(false))

        assertEquals(
            listOf(FakeHub.EPOCH to cursor.toString(), EPOCH_2 to "0"),
            hub.asked("GET", "/v1/changes").map { it.headers["CampusLife-Epoch"] to it.query["since"] },
        )
        val resent = hub.asked("POST", "/v1/mutations").single()
        assertEquals(EPOCH_2, resent.headers["CampusLife-Epoch"])
        assertEquals(listOf(kept, lost), resent.json().getValue("mutations").jsonArray.map { it.jsonObject.getValue("id").jsonPrimitive.content })
        assertNotNull(hub.row("item", lost))
        assertNotNull(db.cache().get("item", kept))
        assertNotNull(db.cache().get("item", lost))
        assertEquals(EPOCH_2, store.standing()?.epoch)
    }

    @Test
    fun `a Hub restored from before a capture gets its Blobs and its commit again in one pass`() {
        val backup = hub.backup()
        val id = sealedCapture(ABC)
        engine().run(false)
        hub.restore(backup, EPOCH_2)
        hub.requests.clear()

        assertEquals(SyncOutcome.Synced(), engine().run(false))

        assertEquals(ABC, hub.stored[ABC_SHA]?.decodeToString())
        assertEquals(2, hub.asked("POST", "/v1/mutations").size)
        assertNotNull(hub.row("capture", id))
        assertNotNull(db.cache().get("capture", id))
        assertEquals(CaptureTransfer.COMMITTED, transferOf(id))
    }

    @Test
    fun `a Hub that no longer takes this contract version stops the sync`() {
        hub.minContractVersion = 2

        assertEquals(SyncOutcome.ContractTooOld, engine().run(false))

        assertNull(store.standing())
    }

    @Test
    fun `contract_too_old on an operation stops the sync and leaves the Outbox as it was`() {
        val course = items.create(course("A"))
        hub.once({ it.path == "/v1/mutations" }, { hub.refusal(it, 400, "contract_too_old") })

        assertEquals(SyncOutcome.ContractTooOld, engine().run(false))

        assertNull(hub.row("item", course))
        assertEquals(1, store.nextBatch().size)
    }

    @Test
    fun `a refused token tells pairing again, revoked and dormant apart`() {
        items.create(course("A"))
        val outcomes = listOf(
            "unauthorized" to SyncOutcome.Unauthorized,
            "device_revoked" to SyncOutcome.Revoked,
            "device_dormant" to SyncOutcome.Dormant,
            "a_code_added_later" to SyncOutcome.Unauthorized,
        )

        for ((code, outcome) in outcomes) {
            hub.refuseToken = code
            assertEquals(code, outcome, engine().run(false))
        }

        assertEquals(1, store.nextBatch().size)
        assertNull(store.standing())
    }

    @Test
    fun `an answer from another Hub stops the sync before anything is sent`() {
        items.create(course("A"))

        assertEquals(SyncOutcome.WrongHub, engine(FakeHub(hubId = OTHER_HUB_ID)).run(false))

        assertNull(store.standing())
        assertEquals(1, store.nextBatch().size)
    }

    @Test
    fun `a store that syncs with another Hub sends nothing to this one`() {
        store.knowHub(OTHER_HUB_ID, FakeHub.EPOCH)

        assertEquals(SyncOutcome.WrongHub, engine().run(false))

        assertEquals(emptyList<FakeHub.Asked>(), hub.requests)
    }

    @Test
    fun `an answer without the Hub's id is a Hub not reached`() {
        hub.once({ it.path == "/v1/hub" }, { hub.response(it, 502, "Bad Gateway", hub = null) })

        assertEquals(SyncOutcome.HubNotReached, engine().run(false))

        assertNull(store.standing())
    }

    @Test
    fun `the heartbeat reports what waits on this device`() {
        hub.storageFull = true
        sealedCapture(ABC)
        val course = items.create(course("A"))

        engine().run(false)

        val status = hub.statuses.single()
        assertEquals("0.1.0", status["app_version"]?.jsonPrimitive?.content)
        assertEquals(1L, status["pending_upload_count"]?.jsonPrimitive?.long)
        assertEquals(1L, status["pending_mutation_count"]?.jsonPrimitive?.long)
        assertEquals(0L, status["rejected_mutation_count"]?.jsonPrimitive?.long)
        assertEquals("2026-03-02T01:00:00.123Z", status["last_successful_sync_at"]?.jsonPrimitive?.content)
        assertEquals(3L, status["outbox_bytes"]?.jsonPrimitive?.long)
        assertEquals(
            db.cache().get("item", course)?.data?.toByteArray()?.size?.toLong(),
            status["cache_bytes"]?.jsonPrimitive?.long,
        )
    }

    @Test
    fun `passes run one at a time, so one Blob's upload events never interleave`() {
        sealedCapture(ABC)
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        hub.once({ it.method == "PUT" }) { request ->
            entered.countDown()
            release.await(5, TimeUnit.SECONDS)
            hub.response(request, 204, "")
        }
        val engine = engine()

        val first = thread(name = "first") { engine.run(false) }
        assertTrue(entered.await(5, TimeUnit.SECONDS))
        val second = thread(name = "second") { engine.run(false) }
        Thread.sleep(200)
        release.countDown()
        first.join()
        second.join()

        val threads = hub.requests.map { it.thread }
        assertTrue("second" in threads)
        assertEquals(threads.sortedBy { it == "second" }, threads)
    }

    @Test(timeout = 10_000)
    fun `an answer that moves no Mutation ends the push`() {
        val course = items.create(course("A"))
        hub.once({ it.path == "/v1/mutations" }) { request ->
            val id = hub.requests.last().json().getValue("mutations").jsonArray.single().jsonObject.getValue("mutation_id").jsonPrimitive.content
            hub.response(request, 200, """{"results":[{"mutation_id":"$id","result":"not_attempted"}]}""")
        }

        assertEquals(SyncOutcome.Synced(), engine().run(false))

        assertNull(hub.row("item", course))
        assertEquals(1, store.nextBatch().size)
    }

    private fun engine(from: FakeHub = hub, largeBlobBytes: Long = LARGE_BLOB_BYTES): SyncEngine {
        val client = HubClient(URL, FakeHub.HUB_ID, FakeHub.TOKEN, OkHttpClient.Builder().addInterceptor(from).build())
        return SyncEngine(store, client, clock, "0.1.0", largeBlobBytes)
    }

    private fun course(title: String) = ItemData(kind = ItemKind.COURSE, title = title, fields = JsonObject(emptyMap()))

    private fun sealedCapture(vararg contents: String): String {
        val id = captures.open(Origin(OriginType.CAMERA))
        for (content in contents) captures.addFile(id, content.byteInputStream(), photo)
        captures.seal(id)
        return id
    }

    private fun transferOf(id: String): CaptureTransfer = runBlocking { captures.observe().first() }.single { it.id == id }.transfer

    private fun commitRow(id: String): OutboxRow =
        db.outbox().inStates(TransferState.entries.map { it.column }).single { it.entity == "capture" && it.entityId == id }

    private fun chunksSent(sha256: String): List<Long> =
        hub.requests.filter { it.method == "PUT" && it.path.startsWith("/v1/blobs/uploads/$sha256/chunks/") }
            .map { it.path.substringAfterLast('/').toLong() }

    private fun blobRequests(): List<String> =
        hub.requests.filter { it.path.startsWith("/v1/blobs/") }.map { "${it.method} ${it.path}" }

    private companion object {
        val URL = "http://hub.test/".toHttpUrl()
        const val EPOCH_2 = "1e2d3c4b-5a69-4788-9a0b-c1d2e3f4a5b6"
        const val OTHER_HUB_ID = "9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a"
        const val ABC = "abc"
        const val ABC_SHA = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        const val NIST_448 = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"
        const val NIST_448_SHA = "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
    }
}
