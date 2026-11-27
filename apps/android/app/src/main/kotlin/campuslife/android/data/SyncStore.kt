package campuslife.android.data

import campuslife.wire.MutationResult
import java.io.File
import java.time.Clock

/** The side of the Room store the sync engine drives: what to upload and send next, and what the Hub answered. */
class SyncStore(
    private val db: CampusLifeDatabase,
    private val files: BlobFiles,
    private val clock: Clock,
) {
    data class Upload(val sha256: String, val size: Long, val state: TransferState, val file: File)

    data class Outgoing(val mutationId: String, val body: String)

    /** Blobs the Hub does not have yet, oldest first. */
    fun uploads(): List<Upload> = TODO()

    fun onUpload(sha256: String, event: BlobEvent): Unit = TODO()

    /** The next batch of POST /v1/mutations: pending Mutations in the order they were made. */
    fun nextBatch(max: Int = MAX_BATCH): List<Outgoing> = TODO()

    fun onResults(results: List<MutationResult>): Unit = TODO()

    companion object {
        const val MAX_BATCH = 200
    }
}
