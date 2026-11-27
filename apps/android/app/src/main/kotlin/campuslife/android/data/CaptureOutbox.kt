package campuslife.android.data

import campuslife.wire.AttachmentRole
import campuslife.wire.CaptureCommit
import campuslife.wire.CapturedAtPrecision
import campuslife.wire.CaptureLink
import campuslife.wire.CloudPolicy
import campuslife.wire.CommitAttachment
import campuslife.wire.CommitCapture
import campuslife.wire.Origin
import campuslife.wire.Timeline
import campuslife.wire.wireJson
import java.io.InputStream
import java.time.Clock
import java.time.Instant
import java.util.UUID
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

/** The write seam for Captures (decision 5): ids, Blob files and the capture.commit are made in here. */
class CaptureOutbox(
    private val db: CampusLifeDatabase,
    private val files: BlobFiles,
    private val clock: Clock,
) {
    /** One file of a Capture; [filename] is the name as received. */
    data class NewFile(
        val role: AttachmentRole,
        val cloudPolicy: CloudPolicy,
        val filename: String? = null,
        val declaredMime: String? = null,
        val seq: Long? = null,
        val capturedAt: Instant? = null,
        val capturedAtPrecision: CapturedAtPrecision? = null,
        val capturedMonoNs: Long? = null,
    )

    data class CaptureView(val id: String, val sealed: Boolean, val transfer: CaptureTransfer, val attachments: Int)

    /** The transfer state of a Capture (D1), read from its Blobs and its capture.commit. */
    enum class CaptureTransfer { PENDING, UPLOADING, RETRY_WAIT, COMMITTED, NEEDS_ATTENTION }

    fun open(
        origin: Origin,
        links: List<CaptureLink> = emptyList(),
        categoryHint: String? = null,
        userNote: String? = null,
        url: String? = null,
    ): String {
        for (link in links) require(ITEM_REF.matches(link.to)) { "a link names an item, not ${link.to}" }
        val draft = CaptureCommit(
            id = UUID.randomUUID().toString(),
            createdAt = clock.instant(),
            origin = origin,
            categoryHint = categoryHint,
            userNote = userNote,
            url = url,
            links = links,
            attachments = emptyList(),
        )
        val text = wireJson.encodeToJsonElement(CaptureCommit.serializer(), draft).nfcStrings().toString()
        db.captures().add(LocalCapture(draft.id, OPEN, text))
        return draft.id
    }

    /** Copies [source] into the Blob store and adds it to the open Capture; returns the Attachment id. */
    fun addFile(captureId: String, source: InputStream, file: NewFile): String {
        requireOpen(captureId)
        val kept = files.put(source)
        val name = file.filename?.takeIf(String::isNotEmpty)
        val canonical = name?.let { nfc(it) }
        val attachment = CommitAttachment(
            id = UUID.randomUUID().toString(),
            blobSha256 = kept.sha256,
            role = file.role,
            seq = file.seq,
            originalFilename = canonical,
            originalFilenameRaw = name?.takeIf { it != canonical },
            declaredMime = file.declaredMime,
            capturedAt = file.capturedAt,
            capturedAtPrecision = file.capturedAtPrecision,
            capturedMonoNs = file.capturedMonoNs,
            cloudPolicy = file.cloudPolicy,
        )
        db.inTransaction {
            requireOpen(captureId)
            val body = wireJson.encodeToString(CommitAttachment.serializer(), attachment)
            val position = db.captures().attachmentCount(captureId)
            db.captures().addAttachment(LocalAttachment(attachment.id, captureId, position, kept.sha256, body))
            db.blobs().addIfAbsent(BlobRow(kept.sha256, kept.size, TransferState.WAITING.column))
        }
        return attachment.id
    }

    /** Seals the Capture; its capture.commit goes out once every Blob is stored (decision 6). Sealing again does nothing. */
    fun seal(captureId: String, timeline: Timeline? = null): Unit = db.inTransaction {
        val capture = requireNotNull(db.captures().get(captureId)) { "no capture $captureId" }
        if (capture.seal == SEALED) return@inTransaction
        val draft = wireJson.decodeFromString(CaptureCommit.serializer(), capture.draft)
        val attachments = db.captures().attachments(captureId)
            .map { wireJson.decodeFromString(CommitAttachment.serializer(), it.body) }
        db.captures().setSeal(captureId, SEALED)
        val commit = draft.copy(attachments = attachments, timeline = timeline)
        db.enqueue(CommitCapture(UUID.randomUUID().toString(), commit), TransferState.HELD)
        db.settleHeldCommits()
    }

    fun observe(): Flow<List<CaptureView>> = db.captures().observeSummaries().map { rows -> rows.map(::view) }

    private fun requireOpen(captureId: String) {
        val capture = requireNotNull(db.captures().get(captureId)) { "no capture $captureId" }
        check(capture.seal == OPEN) { "capture $captureId is sealed; what comes after goes in a new Capture (D7)" }
    }

    private fun view(row: CaptureSummary) = CaptureView(row.id, row.seal == SEALED, transferOf(row), row.attachments)

    private fun transferOf(row: CaptureSummary): CaptureTransfer {
        val commit = row.commitState?.let(TransferState::of)
        return when {
            commit == TransferState.REJECTED -> CaptureTransfer.NEEDS_ATTENTION
            commit != null && commit in COMMITTED -> CaptureTransfer.COMMITTED
            row.retrying > 0 -> CaptureTransfer.RETRY_WAIT
            row.uploading > 0 || commit == TransferState.PENDING -> CaptureTransfer.UPLOADING
            else -> CaptureTransfer.PENDING
        }
    }

    private companion object {
        const val OPEN = "open"
        const val SEALED = "sealed"
        val COMMITTED = setOf(TransferState.ACKED, TransferState.PULLING, TransferState.SETTLED)
        val ITEM_REF = Regex("item:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
    }
}
