package campuslife.android.data

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import campuslife.android.data.CaptureOutbox.CaptureTransfer
import campuslife.android.data.CaptureOutbox.NewFile
import campuslife.wire.AttachmentRole
import campuslife.wire.CaptureLink
import campuslife.wire.CaptureLinkType
import campuslife.wire.CloudPolicy
import campuslife.wire.CommitCapture
import campuslife.wire.MutationOutcome
import campuslife.wire.MutationResult
import campuslife.wire.MutationSerializer
import campuslife.wire.Origin
import campuslife.wire.OriginType
import campuslife.wire.RejectReason
import campuslife.wire.wireJson
import java.io.File
import java.text.Normalizer
import java.time.Clock
import java.time.Instant
import java.time.ZoneOffset
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class CaptureTransferTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val clock = Clock.fixed(Instant.parse("2026-03-02T01:00:00.123Z"), ZoneOffset.UTC)
    private val photo = NewFile(role = AttachmentRole.PHOTO, cloudPolicy = CloudPolicy.LOCAL_ONLY, declaredMime = "image/jpeg")
    private lateinit var blobDir: File
    private lateinit var db: CampusLifeDatabase
    private lateinit var captures: CaptureOutbox
    private lateinit var sync: SyncStore

    @Before
    fun open() {
        blobDir = folder.newFolder("blobs")
        reopen()
    }

    @After
    fun close() = db.close()

    /** Closes the store and opens it again on the same files, as a new process would after the old one died. */
    private fun reopen() {
        if (::db.isInitialized) db.close()
        db = Room.databaseBuilder(context, CampusLifeDatabase::class.java, File(folder.root, "store.db").path)
            .allowMainThreadQueries()
            .build()
        val files = BlobFiles(blobDir)
        captures = CaptureOutbox(db, files, clock)
        sync = SyncStore(db, files, clock)
    }

    @Test
    fun `a sealed capture is committed once every blob is stored`() {
        val id = captures.open(Origin(OriginType.CAMERA), links = listOf(CaptureLink(CaptureLinkType.ABOUT, "item:$SESSION")))
        captures.addFile(id, ABC.byteInputStream(), photo)
        captures.addFile(id, NIST_448.byteInputStream(), photo.copy(role = AttachmentRole.FILE))
        captures.seal(id)

        assertEquals(emptyList<SyncStore.Outgoing>(), sync.nextBatch())
        assertEquals(CaptureTransfer.PENDING, transferOf(id))
        assertEquals(listOf(ABC_SHA to 3L, NIST_448_SHA to 56L), sync.uploads().map { it.sha256 to it.size })

        storeEveryBlob()
        val commit = commits().single()

        assertEquals(id, commit.capture.id)
        assertEquals(Instant.parse("2026-03-02T01:00:00.123Z"), commit.capture.createdAt)
        assertEquals(listOf(CaptureLink(CaptureLinkType.ABOUT, "item:$SESSION")), commit.capture.links)
        assertEquals(listOf(ABC_SHA, NIST_448_SHA), commit.capture.attachments.map { it.blobSha256 })
        assertEquals(listOf(AttachmentRole.PHOTO, AttachmentRole.FILE), commit.capture.attachments.map { it.role })
        assertEquals(CaptureTransfer.UPLOADING, transferOf(id))

        sync.onResults(listOf(applied(commit.mutationId)))

        assertCommitted(id)
    }

    @Test
    fun `a link names an item`() {
        for (to in listOf(SESSION, "tag:$SESSION", "item:${SESSION.uppercase()}")) {
            assertThrows(to, IllegalArgumentException::class.java) {
                captures.open(Origin(OriginType.CAMERA), links = listOf(CaptureLink(CaptureLinkType.ABOUT, to)))
            }
        }
        assertEquals(emptyList<CaptureOutbox.CaptureView>(), views())
    }

    @Test
    fun `blobs upload while the capture is still open`() {
        val id = captures.open(Origin(OriginType.RECORD))
        captures.addFile(id, ABC.byteInputStream(), photo)

        assertEquals(listOf(ABC_SHA), sync.uploads().map { it.sha256 })
        storeEveryBlob()
        captures.seal(id)

        assertEquals(listOf(id), commits().map { it.capture.id })
    }

    @Test
    fun `the copy is hashed on the way in and the same bytes are kept and uploaded once`() {
        val first = captures.open(Origin(OriginType.SHARE))
        captures.addFile(first, ABC.byteInputStream(), photo)
        val second = captures.open(Origin(OriginType.SHARE))
        captures.addFile(second, ABC.byteInputStream(), photo)

        val upload = sync.uploads().single()
        assertEquals(ABC_SHA, upload.sha256)
        assertEquals(ABC, upload.file.readText())
        assertEquals(listOf(ABC_SHA), blobDir.list()!!.toList())
    }

    @Test
    fun `an empty file is a blob too`() {
        val id = captures.open(Origin(OriginType.SHARE))
        captures.addFile(id, "".byteInputStream(), photo)

        assertEquals(listOf(EMPTY_SHA to 0L), sync.uploads().map { it.sha256 to it.size })
    }

    @Test
    fun `a sealed capture takes no more files and sealing again changes nothing`() {
        val id = captures.open(Origin(OriginType.CAMERA))
        captures.addFile(id, ABC.byteInputStream(), photo)
        captures.seal(id)
        captures.seal(id)

        assertThrows(IllegalStateException::class.java) { captures.addFile(id, NIST_448.byteInputStream(), photo) }
        storeEveryBlob()
        assertEquals(1, commits().single().capture.attachments.size)
        assertEquals(listOf(CaptureOutbox.CaptureView(id, sealed = true, transfer = CaptureTransfer.UPLOADING, attachments = 1)), views())
    }

    @Test
    fun `filenames go out NFC and the name as received only when it was not`() {
        val decomposed = Normalizer.normalize("강의 자료.pdf", Normalizer.Form.NFD)
        val id = captures.open(Origin(OriginType.SHARE))
        captures.addFile(id, ABC.byteInputStream(), photo.copy(filename = decomposed))
        captures.addFile(id, NIST_448.byteInputStream(), photo.copy(filename = "notes.txt"))
        captures.seal(id)
        storeEveryBlob()

        val attachments = commits().single().capture.attachments
        assertEquals(listOf("강의 자료.pdf", "notes.txt"), attachments.map { it.originalFilename })
        assertEquals(listOf(decomposed, null), attachments.map { it.originalFilenameRaw })
    }

    @Test
    fun `the text of a capture goes out NFC`() {
        fun decomposed(text: String) = Normalizer.normalize(text, Normalizer.Form.NFD)
        val origin = Origin(
            OriginType.SHARE,
            uri = "content://campuslife.invalid/" + decomposed("공지"),
            app = decomposed("학교 앱"),
            publisher = decomposed("학생처"),
            distributedBy = decomposed("학과 사무실"),
        )
        val id = captures.open(origin, url = "https://campuslife.invalid/" + decomposed("공지"))
        captures.addFile(id, ABC.byteInputStream(), photo)
        captures.seal(id)
        storeEveryBlob()

        val capture = commits().single().capture
        val expected = Origin(
            OriginType.SHARE,
            uri = "content://campuslife.invalid/공지",
            app = "학교 앱",
            publisher = "학생처",
            distributedBy = "학과 사무실",
        )
        assertEquals(expected, capture.origin)
        assertEquals("https://campuslife.invalid/공지", capture.url)
    }

    @Test
    fun `an upload session that expired is opened again and the capture still commits`() {
        val id = sealedCapture(ABC)
        sync.onUpload(ABC_SHA, BlobEvent.SESSION_OPENED)
        assertEquals(TransferState.UPLOADING, sync.uploads().single().state)

        sync.onUpload(ABC_SHA, BlobEvent.SESSION_LOST)
        assertEquals(TransferState.WAITING, sync.uploads().single().state)

        sync.onUpload(ABC_SHA, BlobEvent.SESSION_OPENED)
        sync.onUpload(ABC_SHA, BlobEvent.HUB_HAS_BLOB)
        sync.onResults(commits().map { applied(it.mutationId) })

        assertCommitted(id)
    }

    @Test
    fun `a full Hub leaves the blob waiting to be tried again`() {
        val id = sealedCapture(ABC)
        sync.onUpload(ABC_SHA, BlobEvent.SESSION_OPENED)
        sync.onUpload(ABC_SHA, BlobEvent.DEFERRED)

        assertEquals(CaptureTransfer.RETRY_WAIT, transferOf(id))
        assertEquals(listOf(ABC_SHA to TransferState.RETRY_WAIT), sync.uploads().map { it.sha256 to it.state })
        assertEquals(emptyList<SyncStore.Outgoing>(), sync.nextBatch())

        sync.onUpload(ABC_SHA, BlobEvent.SESSION_OPENED)
        sync.onUpload(ABC_SHA, BlobEvent.HUB_HAS_BLOB)
        sync.onResults(commits().map { applied(it.mutationId) })

        assertCommitted(id)
    }

    @Test
    fun `after a crash during the upload the store goes on from what it kept`() {
        val id = sealedCapture(ABC)
        sync.onUpload(ABC_SHA, BlobEvent.SESSION_OPENED)

        reopen()

        assertEquals(listOf(ABC_SHA to TransferState.UPLOADING), sync.uploads().map { it.sha256 to it.state })
        sync.onUpload(ABC_SHA, BlobEvent.SESSION_OPENED)
        sync.onUpload(ABC_SHA, BlobEvent.HUB_HAS_BLOB)
        sync.onResults(commits().map { applied(it.mutationId) })

        assertCommitted(id)
    }

    @Test
    fun `a batch whose answer was lost goes again unchanged and its duplicate answer is the ACK`() {
        val id = sealedCapture(ABC)
        storeEveryBlob()
        val sent = sync.nextBatch()

        reopen()

        assertEquals(sent, sync.nextBatch())
        val mutationId = sent.single().mutationId
        sync.onResults(listOf(MutationResult(mutationId, MutationOutcome.DUPLICATE, original = applied(mutationId))))

        assertCommitted(id)
    }

    @Test
    fun `a rejected commit needs attention and the rest of the outbox goes on`() {
        val rejected = sealedCapture(ABC)
        val duplicateOfRejected = sealedCapture(EMPTY)
        val accepted = sealedCapture(NIST_448)
        storeEveryBlob()
        val (first, second, third) = commits().map { it.mutationId }

        sync.onResults(
            listOf(
                MutationResult(first, MutationOutcome.REJECTED, reason = RejectReason.LINK_TARGET_MISSING),
                MutationResult(
                    second,
                    MutationOutcome.DUPLICATE,
                    original = MutationResult(second, MutationOutcome.REJECTED, reason = RejectReason.SCHEMA),
                ),
                applied(third),
            ),
        )

        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(rejected))
        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(duplicateOfRejected))
        assertCommitted(accepted)
    }

    @Test
    fun `Mutations after a retry in the batch are sent again`() {
        sealedCapture(ABC)
        val second = sealedCapture(NIST_448)
        storeEveryBlob()
        val (first, rest) = sync.nextBatch().map { it.mutationId }

        sync.onResults(listOf(applied(first), MutationResult(rest, MutationOutcome.NOT_ATTEMPTED)))

        assertEquals(listOf(rest), sync.nextBatch().map { it.mutationId })
        assertEquals(CaptureTransfer.UPLOADING, transferOf(second))
    }

    @Test
    fun `retry missing_blobs uploads those blobs again and then sends the same commit`() {
        val id = sealedCapture(ABC, NIST_448)
        storeEveryBlob()
        val sent = sync.nextBatch().single()

        sync.onResults(listOf(MutationResult(sent.mutationId, MutationOutcome.RETRY, missingBlobs = listOf(NIST_448_SHA))))

        assertEquals(emptyList<SyncStore.Outgoing>(), sync.nextBatch())
        assertEquals(listOf(NIST_448_SHA to TransferState.WAITING), sync.uploads().map { it.sha256 to it.state })
        assertEquals(CaptureTransfer.PENDING, transferOf(id))

        storeEveryBlob()

        assertEquals(listOf(sent), sync.nextBatch())
        sync.onResults(listOf(applied(sent.mutationId)))
        assertCommitted(id)
    }

    @Test
    fun `retry missing_blobs for a blob whose local copy is gone puts the commit in needs attention`() {
        val id = sealedCapture(ABC)
        storeEveryBlob()
        val sent = sync.nextBatch().single()
        File(blobDir, ABC_SHA).delete()

        sync.onResults(listOf(MutationResult(sent.mutationId, MutationOutcome.RETRY, missingBlobs = listOf(ABC_SHA))))

        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(id))
        assertEquals(emptyList<SyncStore.Outgoing>(), sync.nextBatch())
        assertEquals(emptyList<SyncStore.Upload>(), sync.uploads())
    }

    @Test
    fun `a held capture whose blob copy is gone needs attention`() {
        val id = sealedCapture(ABC, NIST_448)
        sync.onUpload(ABC_SHA, BlobEvent.HUB_HAS_BLOB)
        File(blobDir, NIST_448_SHA).delete()

        sync.onUpload(NIST_448_SHA, BlobEvent.COPY_LOST)

        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(id))
        assertEquals(emptyList<SyncStore.Upload>(), sync.uploads())
        assertEquals(emptyList<SyncStore.Outgoing>(), sync.nextBatch())
    }

    @Test
    fun `a copy that no longer holds its bytes is dropped, and the same bytes added again upload`() {
        val id = sealedCapture(ABC)
        File(blobDir, ABC_SHA).writeText("abd")

        sync.onUpload(ABC_SHA, BlobEvent.COPY_LOST)

        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(id))
        val again = sealedCapture(ABC)
        assertEquals(listOf(ABC_SHA to ABC), sync.uploads().map { it.sha256 to it.file.readText() })
        storeEveryBlob()
        assertEquals(listOf(again), commits().map { it.capture.id })
    }

    @Test
    fun `a copy that still holds its bytes is uploaded again when the Hub found the bytes it got different`() {
        val id = sealedCapture(ABC)
        sync.onUpload(ABC_SHA, BlobEvent.SESSION_OPENED)

        sync.onUpload(ABC_SHA, BlobEvent.COPY_LOST)

        assertEquals(ABC, File(blobDir, ABC_SHA).readText())
        assertEquals(listOf(ABC_SHA to TransferState.WAITING), sync.uploads().map { it.sha256 to it.state })
        assertEquals(CaptureTransfer.PENDING, transferOf(id))
        storeEveryBlob()
        assertEquals(listOf(id), commits().map { it.capture.id })
    }

    @Test
    fun `bytes added again over a copy that no longer holds them replace it`() {
        val id = captures.open(Origin(OriginType.CAMERA))
        captures.addFile(id, ABC.byteInputStream(), photo)
        File(blobDir, ABC_SHA).writeText("abd")

        captures.addFile(id, ABC.byteInputStream(), photo)

        assertEquals(ABC, File(blobDir, ABC_SHA).readText())
        assertEquals(listOf(ABC_SHA), blobDir.list()!!.toList())
    }

    @Test
    fun `a capture sealed after its blob copy was lost needs attention`() {
        val id = captures.open(Origin(OriginType.CAMERA))
        captures.addFile(id, ABC.byteInputStream(), photo)
        File(blobDir, ABC_SHA).delete()
        sync.onUpload(ABC_SHA, BlobEvent.COPY_LOST)

        captures.seal(id)

        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(id))
    }

    @Test
    fun `a stored blob stays stored whatever happens to the copy`() {
        val id = sealedCapture(ABC, NIST_448)
        sync.onUpload(ABC_SHA, BlobEvent.HUB_HAS_BLOB)
        File(blobDir, ABC_SHA).delete()

        sync.onUpload(ABC_SHA, BlobEvent.COPY_LOST)

        assertEquals(TransferState.STORED.column, db.blobs().get(ABC_SHA)?.state)
        assertEquals(CaptureTransfer.PENDING, transferOf(id))
        sync.onUpload(NIST_448_SHA, BlobEvent.HUB_HAS_BLOB)
        sync.onResults(listOf(applied(sync.nextBatch().single().mutationId)))
        assertCommitted(id)
    }

    @Test
    fun `a blob another capture still holds stays for it when one commit finds the copy gone`() {
        val first = sealedCapture(ABC)
        val second = captures.open(Origin(OriginType.CAMERA))
        captures.addFile(second, ABC.byteInputStream(), photo)
        storeEveryBlob()
        val sent = sync.nextBatch().single()
        File(blobDir, ABC_SHA).delete()

        sync.onResults(listOf(MutationResult(sent.mutationId, MutationOutcome.RETRY, missingBlobs = listOf(ABC_SHA))))

        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(first))
        assertEquals(listOf(ABC_SHA to TransferState.WAITING), sync.uploads().map { it.sha256 to it.state })

        captures.seal(second)
        sync.onUpload(ABC_SHA, BlobEvent.COPY_LOST)

        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(second))
        assertEquals(emptyList<SyncStore.Upload>(), sync.uploads())
    }

    @Test
    fun `a blob a held commit still needs stays for it when another commit finds the copy gone`() {
        val first = sealedCapture(ABC)
        val second = sealedCapture(ABC, NIST_448)
        sync.onUpload(ABC_SHA, BlobEvent.HUB_HAS_BLOB)
        val sent = sync.nextBatch().single()
        File(blobDir, ABC_SHA).delete()

        sync.onResults(listOf(MutationResult(sent.mutationId, MutationOutcome.RETRY, missingBlobs = listOf(ABC_SHA))))

        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(first))
        assertEquals(CaptureTransfer.PENDING, transferOf(second))
        assertEquals(setOf(ABC_SHA, NIST_448_SHA), sync.uploads().map { it.sha256 }.toSet())

        sync.onUpload(ABC_SHA, BlobEvent.COPY_LOST)

        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(second))
    }

    @Test
    fun `a blob a pending commit still needs stays for it when another commit finds the copy gone`() {
        val first = sealedCapture(ABC)
        val second = sealedCapture(ABC)
        storeEveryBlob()
        val (one, two) = sync.nextBatch().map { it.mutationId }
        File(blobDir, ABC_SHA).delete()

        sync.onResults(
            listOf(
                MutationResult(one, MutationOutcome.RETRY, missingBlobs = listOf(ABC_SHA)),
                MutationResult(two, MutationOutcome.NOT_ATTEMPTED),
            ),
        )

        assertEquals(CaptureTransfer.NEEDS_ATTENTION, transferOf(first))
        assertEquals(listOf(ABC_SHA to TransferState.WAITING), sync.uploads().map { it.sha256 to it.state })
        assertEquals(listOf(two), sync.nextBatch().map { it.mutationId })
        assertEquals(CaptureTransfer.UPLOADING, transferOf(second))
    }

    @Test
    fun `an answer for a Mutation that is no longer pending changes nothing`() {
        val id = sealedCapture(ABC)
        storeEveryBlob()
        val mutationId = sync.nextBatch().single().mutationId
        sync.onResults(listOf(applied(mutationId)))

        sync.onResults(listOf(MutationResult(mutationId, MutationOutcome.REJECTED, reason = RejectReason.SCHEMA)))
        sync.onResults(listOf(MutationResult(mutationId, MutationOutcome.RETRY, missingBlobs = listOf(ABC_SHA))))

        assertCommitted(id)
    }

    private fun sealedCapture(vararg contents: String): String {
        val id = captures.open(Origin(OriginType.CAMERA))
        for (content in contents) captures.addFile(id, content.byteInputStream(), photo)
        captures.seal(id)
        return id
    }

    private fun storeEveryBlob() {
        for (upload in sync.uploads()) sync.onUpload(upload.sha256, BlobEvent.HUB_HAS_BLOB)
    }

    private fun commits(): List<CommitCapture> =
        sync.nextBatch().map { wireJson.decodeFromString(MutationSerializer, it.body) as CommitCapture }

    private fun views(): List<CaptureOutbox.CaptureView> = runBlocking { captures.observe().first() }

    private fun transferOf(id: String): CaptureTransfer = views().single { it.id == id }.transfer

    private fun assertCommitted(id: String) {
        assertEquals(CaptureTransfer.COMMITTED, transferOf(id))
        assertEquals(emptyList<SyncStore.Upload>(), sync.uploads())
        assertEquals(emptyList<SyncStore.Outgoing>(), sync.nextBatch())
    }

    private fun applied(mutationId: String) = MutationResult(mutationId, MutationOutcome.APPLIED)

    private companion object {
        const val SESSION = "3b241101-e2bb-4255-8caf-4136c566a962"
        const val ABC = "abc"
        const val ABC_SHA = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        const val NIST_448 = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"
        const val NIST_448_SHA = "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        const val EMPTY = ""
        const val EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    }
}
