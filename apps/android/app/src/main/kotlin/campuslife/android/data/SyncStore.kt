package campuslife.android.data

import campuslife.wire.ChangesPage
import campuslife.wire.EntityName
import campuslife.wire.MutationOutcome
import campuslife.wire.MutationResult
import campuslife.wire.SENT_RETENTION_DAYS
import campuslife.wire.wireJson
import java.io.File
import java.time.Clock
import java.time.Duration

/** The side of the Room store the sync engine drives: what to upload and send next, and what the Hub answered. */
class SyncStore(
    private val db: CampusLifeDatabase,
    private val files: BlobFiles,
    private val clock: Clock,
) {
    data class Upload(val sha256: String, val size: Long, val state: TransferState, val file: File)

    data class Outgoing(val mutationId: String, val body: String)

    /** What the heartbeat reports of this device (device/status). */
    data class Backlog(
        val pendingCaptures: Long,
        val pendingMutations: Long,
        val rejectedMutations: Long,
        val cacheBytes: Long,
        val blobBytes: Long,
    )

    /** The Hub this store syncs with, its base epoch and the feed cursor; null before the first GET /v1/hub. */
    fun standing(): SyncMeta? = db.syncMeta().get()

    /** Takes the epoch of the first GET /v1/hub as the base epoch; only [epochChanged] changes it later (D24). */
    fun knowHub(hubId: String, epoch: String): Unit = db.inTransaction {
        if (db.syncMeta().get() == null) db.syncMeta().put(SyncMeta(hubId = hubId, epoch = epoch, cursor = 0))
    }

    /**
     * The Hub answered 409 epoch_changed, as after a restore (D24): the cache goes, the feed is read from 0 under
     * [epoch], and every Mutation the Hub answered or that was set aside as rejected goes to it again ahead of the
     * unsent ones, each group in its first order. The restored Hub then tells which it still has (duplicate) from
     * those it lost, and may take what an earlier Hub refused, as a commit whose Blob only this backup holds.
     */
    fun epochChanged(epoch: String): Unit = db.inTransaction {
        check(db.syncMeta().restart(epoch) == 1) { "the epoch changed before the Hub is known" }
        db.cache().clear()
        val unsent = db.outbox().inStates(UNSENT)
        for (row in db.outbox().inStates(ANSWERED)) {
            db.outbox().requeue(row.seq, TransferState.of(row.state).after(OutboxEvent.EPOCH_CHANGED).column)
        }
        for (row in unsent) {
            db.outbox().remove(row.seq)
            db.outbox().add(row.copy(seq = 0))
        }
    }

    /**
     * The user confirmed pairing with a Hub other than the one this store synced with (decision B): the meta and the
     * cache go, and so do the Mutations the previous Hub answered; those it refused go to the new Hub with the unsent.
     * The next pass takes the new Hub's epoch as the base epoch.
     */
    fun changeHub(): Unit = TODO()

    /**
     * Keeps why the sync stopped until the user acts; passes answer with it and ask the Hub nothing until [resume].
     * Before the first GET /v1/hub there is no standing to keep it with, so nothing is kept.
     */
    fun stop(reason: String) {
        db.syncMeta().setStopped(reason)
    }

    /** The user acted on a stop, as by pairing again, or the stop no longer holds: the next pass asks the Hub again. */
    fun resume() {
        db.syncMeta().setStopped(null)
    }

    /** Sets a Mutation aside that makes the Hub refuse any batch it is in; the user sees it with the rejected. */
    fun refuse(mutationId: String): Unit = db.inTransaction {
        val row = db.outbox().byMutationId(mutationId) ?: return@inTransaction
        val to = TransferState.of(row.state).after(OutboxEvent.UNSENDABLE)
        if (to.column != row.state) db.outbox().move(row.seq, to.column)
    }

    /** Forgets settled Mutations answered more than SENT_RETENTION_DAYS ago (D24). */
    fun forgetSent(): Int = db.outbox().forget(clock.millis() - Duration.ofDays(SENT_RETENTION_DAYS).toMillis())

    fun backlog(): Backlog = Backlog(
        pendingCaptures = db.captures().unacknowledged(),
        pendingMutations = db.outbox().count(UNSENT),
        rejectedMutations = db.outbox().count(listOf(TransferState.REJECTED.column)),
        cacheBytes = db.cache().bytes(),
        blobBytes = db.blobs().bytes(),
    )

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

    /**
     * Writes one changes page over entity_cache and moves the cursor; the last page finishes the pull.
     * Rows of every entity are kept, so a later version reads the ones this version does not without a since=0.
     */
    fun applyPage(page: ChangesPage): Unit = db.inTransaction {
        check(db.syncMeta().setCursor(page.nextSince) == 1) { "a changes page arrived before the Hub is known" }
        for (change in page.changes) {
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

    /**
     * The uploader found the copy of a Blob the Hub does not have yet gone, or the Hub found the bytes it got
     * different. Hashed again here, a copy that still holds its bytes is uploaded again; otherwise the file
     * and its row go.
     */
    private fun lose(sha256: String) {
        if (db.blobs().get(sha256)?.state.let { it == null || it == TransferState.STORED.column }) return
        if (files.verify(sha256)) return db.inTransaction { moveBlob(sha256, BlobEvent.SESSION_LOST) }
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

        private val UNSENT = listOf(TransferState.HELD.column, TransferState.PENDING.column)

        private val ANSWERED = listOf(
            TransferState.ACKED.column,
            TransferState.PULLING.column,
            TransferState.SETTLED.column,
            TransferState.REJECTED.column,
        )
    }
}
