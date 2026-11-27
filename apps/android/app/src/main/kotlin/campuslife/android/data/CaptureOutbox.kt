package campuslife.android.data

import campuslife.wire.AttachmentRole
import campuslife.wire.CapturedAtPrecision
import campuslife.wire.CaptureLink
import campuslife.wire.CloudPolicy
import campuslife.wire.Origin
import campuslife.wire.Timeline
import java.io.InputStream
import java.time.Clock
import java.time.Instant
import kotlinx.coroutines.flow.Flow

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
    ): String = TODO()

    /** Copies [source] into the Blob store and adds it to the open Capture; returns the Attachment id. */
    fun addFile(captureId: String, source: InputStream, file: NewFile): String = TODO()

    /** Seals the Capture; its capture.commit goes out once every Blob is stored (decision 6). Sealing again does nothing. */
    fun seal(captureId: String, timeline: Timeline? = null): Unit = TODO()

    fun observe(): Flow<List<CaptureView>> = TODO()
}
