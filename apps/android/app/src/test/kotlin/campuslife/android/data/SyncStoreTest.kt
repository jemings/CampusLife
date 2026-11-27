package campuslife.android.data

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import campuslife.android.data.CaptureOutbox.NewFile
import campuslife.wire.AttachmentRole
import campuslife.wire.ChangesPage
import campuslife.wire.CloudPolicy
import campuslife.wire.ItemData
import campuslife.wire.ItemKind
import campuslife.wire.MutationOutcome
import campuslife.wire.MutationResult
import campuslife.wire.Origin
import campuslife.wire.OriginType
import campuslife.wire.RejectReason
import java.io.File
import java.time.Clock
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset
import kotlinx.serialization.json.JsonObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class SyncStoreTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val clock = MovingClock(Instant.parse("2026-03-02T01:00:00.123Z"))
    private val photo = NewFile(role = AttachmentRole.PHOTO, cloudPolicy = CloudPolicy.LOCAL_ONLY, declaredMime = "image/jpeg")
    private lateinit var blobDir: File
    private lateinit var db: CampusLifeDatabase
    private lateinit var captures: CaptureOutbox
    private lateinit var items: UserData
    private lateinit var sync: SyncStore

    @Before
    fun open() {
        blobDir = folder.newFolder("blobs")
        db = Room.inMemoryDatabaseBuilder(context, CampusLifeDatabase::class.java).allowMainThreadQueries().build()
        val files = BlobFiles(blobDir)
        captures = CaptureOutbox(db, files, clock)
        items = UserData(db)
        sync = SyncStore(db, files, clock)
        sync.knowHub(HUB_ID, EPOCH)
    }

    @After
    fun close() = db.close()

    @Test
    fun `the base epoch is the first one the Hub named`() {
        sync.knowHub(HUB_ID, EPOCH_2)

        assertEquals(SyncMeta(hubId = HUB_ID, epoch = EPOCH, cursor = 0), sync.standing())
    }

    @Test
    fun `a changed epoch sends what the Hub answered again ahead of what it never saw, each in its first order`() {
        val settled = create("settled")
        val held = sealedCapture(ABC)
        val refused = create("refused")
        val acked = create("acked")
        answer(settled, MutationOutcome.APPLIED)
        answer(refused, MutationOutcome.REJECTED)
        sync.beginPull()
        sync.applyPage(lastPage(cursor = 7))
        answer(acked, MutationOutcome.APPLIED)
        val unsent = create("unsent")

        sync.epochChanged(EPOCH_2)
        storeBlob(ABC_SHA)

        assertEquals(listOf(settled, refused, acked, commitOf(held), unsent), sync.nextBatch().map { it.mutationId })
        assertEquals(SyncMeta(hubId = HUB_ID, epoch = EPOCH_2, cursor = 0), sync.standing())
    }

    @Test
    fun `a changed epoch leaves aside what the Hub never judged`() {
        val gone = sealedCapture(ABC)
        storeBlob(ABC_SHA)
        assertEquals(listOf(commitOf(gone)), sync.nextBatch().map { it.mutationId })
        File(blobDir, ABC_SHA).delete()
        sync.onResults(listOf(MutationResult(commitOf(gone), MutationOutcome.RETRY, missingBlobs = listOf(ABC_SHA))))
        val unreadable = create("unreadable")
        sync.refuse(unreadable)

        sync.epochChanged(EPOCH_2)

        assertEquals(emptyList<SyncStore.Outgoing>(), sync.nextBatch())
        assertEquals(2L, sync.backlog().rejectedMutations)
    }

    @Test
    fun `handling the same epoch change twice ends where handling it once does`() {
        val first = create("first")
        answer(first, MutationOutcome.APPLIED)
        val held = sealedCapture(ABC)
        val second = create("second")

        sync.epochChanged(EPOCH_2)
        val once = sync.nextBatch().map { it.mutationId }
        sync.epochChanged(EPOCH_2)
        storeBlob(ABC_SHA)

        assertEquals(listOf(first, second), once)
        assertEquals(listOf(first, commitOf(held), second), sync.nextBatch().map { it.mutationId })
    }

    @Test
    fun `a changed epoch empties the cache and reads the feed from its start`() {
        val id = items.create(ItemData(kind = ItemKind.COURSE, title = "A", fields = JsonObject(emptyMap())))
        db.cache().put(CachedRow("item", id, 3, """{"id":"$id"}"""))
        sync.applyPage(lastPage(cursor = 3))

        sync.epochChanged(EPOCH_2)

        assertNull(db.cache().get("item", id))
        assertEquals(0L, sync.standing()?.cursor)
    }

    @Test
    fun `an answered Mutation is forgotten SENT_RETENTION_DAYS after its answer, a capture commit is kept`() {
        val edit = create("edit")
        val capture = sealedCapture(ABC)
        storeBlob(ABC_SHA)
        answer(edit, MutationOutcome.APPLIED)
        answer(commitOf(capture), MutationOutcome.APPLIED)
        sync.beginPull()
        sync.applyPage(lastPage(cursor = 2))

        clock.now = clock.now.plus(Duration.ofDays(60))
        assertEquals(0, sync.forgetSent())
        clock.now = clock.now.plusMillis(1)
        assertEquals(1, sync.forgetSent())

        assertEquals(listOf(commitOf(capture)), db.outbox().inState(TransferState.SETTLED.column).map { it.mutationId })
        assertNull(db.outbox().byMutationId(edit))
    }

    @Test
    fun `the backlog counts what waits here`() {
        captures.open(Origin(OriginType.CAMERA))
        sealedCapture(ABC)
        val sent = create("sent")
        answer(sent, MutationOutcome.APPLIED)
        val refused = create("refused")
        answer(refused, MutationOutcome.REJECTED)
        create("waiting")
        db.cache().put(CachedRow("item", "x", 1, "{\"title\":\"가\"}"))

        assertEquals(
            SyncStore.Backlog(pendingCaptures = 2, pendingMutations = 2, rejectedMutations = 1, cacheBytes = 15, blobBytes = 3),
            sync.backlog(),
        )
    }

    private fun create(title: String): String {
        val id = items.create(ItemData(kind = ItemKind.COURSE, title = title, fields = JsonObject(emptyMap())))
        return db.outbox().overlayOf("item", id).single().mutationId
    }

    private fun sealedCapture(content: String): String {
        val id = captures.open(Origin(OriginType.CAMERA))
        captures.addFile(id, content.byteInputStream(), photo)
        captures.seal(id)
        return id
    }

    private fun commitOf(captureId: String): String =
        db.outbox().inStates(TransferState.entries.map { it.column }).single { it.entityId == captureId }.mutationId

    private fun storeBlob(sha256: String) {
        sync.onUpload(sha256, BlobEvent.SESSION_OPENED)
        sync.onUpload(sha256, BlobEvent.HUB_HAS_BLOB)
    }

    private fun answer(mutationId: String, outcome: MutationOutcome) {
        val reason = if (outcome == MutationOutcome.REJECTED) RejectReason.SCHEMA else null
        sync.onResults(listOf(MutationResult(mutationId, outcome, reason = reason)))
    }

    private fun lastPage(cursor: Long) = ChangesPage(HUB_ID, EPOCH, emptyList(), nextSince = cursor, hasMore = false)

    class MovingClock(var now: Instant) : Clock() {
        override fun instant(): Instant = now

        override fun getZone(): ZoneId = ZoneOffset.UTC

        override fun withZone(zone: ZoneId): Clock = this
    }

    private companion object {
        const val HUB_ID = "8a1d6f0e-3c2b-4e5f-9a7b-1c2d3e4f5a6b"
        const val EPOCH = "0d9c8b7a-6f5e-4d3c-8b2a-1f0e9d8c7b6a"
        const val EPOCH_2 = "1e2d3c4b-5a69-4788-9a0b-c1d2e3f4a5b6"
        const val ABC = "abc"
        const val ABC_SHA = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    }
}
