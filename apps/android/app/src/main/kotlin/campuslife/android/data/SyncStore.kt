package campuslife.android.data

import campuslife.wire.ChangesPage
import campuslife.wire.EntityName
import campuslife.wire.MutationOutcome
import campuslife.wire.MutationResult
import campuslife.wire.wireJson
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
    fun uploads(): List<Upload> =
        db.blobs().unstored().map { Upload(it.sha256, it.size, TransferState.of(it.state), files.file(it.sha256)) }

    fun onUpload(sha256: String, event: BlobEvent) {
        if (event == BlobEvent.COPY_LOST) return lose(sha256)
        db.inTransaction { moveBlob(sha256, event) }
    }

    /** The next batch of POST /v1/mutations: pending Mutations in the order they were made. */
    fun nextBatch(max: Int = MAX_BATCH): List<Outgoing> =
        db.outbox().pending(max).map { Outgoing(it.mutationId, it.body) }

    fun onResults(results: List<MutationResult>) = db.inTransaction { results.forEach(::answer) }

    /** Marks the start of a pull: Mutations applied before it leave the overlay when it finishes (decision 5). */
    fun beginPull(): Unit = db.inTransaction { moveAll(TransferState.ACKED, OutboxEvent.PULL_BEGUN) }

    /** Writes one changes page over entity_cache and moves the cursor; the last page finishes the pull. */
    fun applyPage(page: ChangesPage): Unit = db.inTransaction {
        check(db.syncMeta().setCursor(page.nextSince) == 1) { "a changes page arrived before the Hub is known" }
        for (change in page.changes) {
            if (change.entity !in CACHED) continue
            val data = change.data
            if (change.deleted || data == null) {
                db.cache().remove(change.entity.value, change.id)
            } else {
                db.cache().put(CachedRow(change.entity.value, change.id, change.rev, data.toString()))
            }
        }
        if (!page.hasMore) moveAll(TransferState.PULLING, OutboxEvent.PULL_DONE)
    }

    private fun answer(result: MutationResult) {
        val row = db.outbox().byMutationId(result.mutationId) ?: return
        val decided = result.decided()
        val event = when (decided.result) {
            MutationOutcome.APPLIED -> OutboxEvent.APPLIED
            MutationOutcome.CONFLICT ->
                if (decided.appliedFields.isNullOrEmpty()) OutboxEvent.CONFLICTED else OutboxEvent.PARTLY_APPLIED
            MutationOutcome.REJECTED -> OutboxEvent.REFUSED
            MutationOutcome.RETRY -> missingBlobsEvent(row, result.missingBlobs.orEmpty())
            else -> null
        } ?: return
        val from = TransferState.of(row.state)
        val to = from.after(event)
        if (to == from) return
        val missing = result.missingBlobs.orEmpty()
        when (event) {
            OutboxEvent.BLOBS_MISSING -> {
                db.outbox().move(row.seq, to.column)
                for (sha256 in missing) {
                    db.blobs().addIfAbsent(BlobRow(sha256, files.file(sha256).length(), TransferState.STORED.column))
                    moveBlob(sha256, BlobEvent.HUB_LOST_BLOB)
                }
            }
            OutboxEvent.BLOBS_GONE -> {
                db.outbox().end(row.seq, to.column, encode(result), null)
                for (sha256 in missing) {
                    if (files.has(sha256) || db.captures().stillNeed(sha256)) {
                        moveBlob(sha256, BlobEvent.HUB_LOST_BLOB)
                    } else {
                        db.blobs().remove(sha256)
                    }
                }
            }
            OutboxEvent.APPLIED, OutboxEvent.CONFLICTED, OutboxEvent.PARTLY_APPLIED ->
                db.outbox().end(row.seq, to.column, encode(result), clock.millis())
            else -> db.outbox().end(row.seq, to.column, encode(result), null)
        }
    }

    private fun missingBlobsEvent(row: OutboxRow, missing: List<String>): OutboxEvent? = when {
        row.entity != EntityName.CAPTURE.value || missing.isEmpty() -> null
        missing.all(files::has) -> OutboxEvent.BLOBS_MISSING
        else -> OutboxEvent.BLOBS_GONE
    }

    private fun moveBlob(sha256: String, event: BlobEvent) {
        val row = db.blobs().get(sha256) ?: return
        val to = TransferState.of(row.state).after(event)
        if (to.column == row.state) return
        db.blobs().move(sha256, to.column)
        if (to == TransferState.STORED) db.settleHeldCommits()
    }

    /** The copy of a Blob the Hub does not have yet is gone or holds other bytes: the file and its row go. */
    private fun lose(sha256: String) {
        if (db.blobs().get(sha256)?.state.let { it == null || it == TransferState.STORED.column }) return
        files.drop(sha256)
        db.inTransaction {
            if (db.blobs().get(sha256)?.state == TransferState.STORED.column) return@inTransaction
            db.blobs().remove(sha256)
            db.settleHeldCommits()
        }
    }

    private fun moveAll(state: TransferState, event: OutboxEvent) {
        val to = state.after(event)
        for (row in db.outbox().inState(state.column)) db.outbox().move(row.seq, to.column)
    }

    private fun encode(result: MutationResult): String = wireJson.encodeToString(MutationResult.serializer(), result)

    companion object {
        const val MAX_BATCH = 200

        private val CACHED = setOf(
            EntityName.CAPTURE,
            EntityName.ATTACHMENT,
            EntityName.ITEM,
            EntityName.RELATION,
            EntityName.TAG,
            EntityName.ARTIFACT,
            EntityName.SUGGESTION,
        )
    }
}
