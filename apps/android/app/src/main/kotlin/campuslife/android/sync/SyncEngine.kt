package campuslife.android.sync

import campuslife.android.data.BlobEvent
import campuslife.android.data.SyncStore
import campuslife.android.data.SyncStore.Upload
import campuslife.android.data.TransferState
import campuslife.wire.CONTRACT_VERSION
import campuslife.wire.DeviceStatus
import campuslife.wire.ErrorCode
import campuslife.wire.UploadSession
import campuslife.wire.isUuid
import java.io.File
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.file.NoSuchFileException
import java.nio.file.StandardOpenOption
import java.time.Clock
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.longOrNull

/** How a sync pass ended. */
sealed interface SyncOutcome {
    /**
     * Uploaded what it could, pushed and pulled. [largeWaiting]: Blobs over [LARGE_BLOB_BYTES] wait for an
     * unmetered network; [deferred]: Blobs wait for the Hub's space or for a read of their copy to work.
     */
    data class Synced(val largeWaiting: Boolean = false, val deferred: Boolean = false) : SyncOutcome

    /** No Hub to sync with yet. */
    data object NotPaired : SyncOutcome

    /** No answer from the Hub: try again later. */
    data object HubNotReached : SyncOutcome

    /** The Hub answered with an error this client cannot act on now: try again later. */
    data object HubFailed : SyncOutcome

    /** The Hub has not seen this Device for its dormant period; it syncs again once approved on the Hub (D23). */
    data object Dormant : SyncOutcome

    /** The Hub does not know the token: pair again. */
    data object Unauthorized : SyncOutcome

    /** The Device was revoked: pair again as a new Device. */
    data object Revoked : SyncOutcome

    /** The answer came from a Hub other than the paired one; syncing stops (D3). */
    data object WrongHub : SyncOutcome

    /** The Hub no longer accepts this app's contract version: update the app. */
    data object ContractTooOld : SyncOutcome
}

/** D7: a Blob over 50 MB goes only over an unmetered network, unless the user allows it on any. */
const val LARGE_BLOB_BYTES = 50_000_000L

/**
 * One device's sync with its Hub (D3, D7, D24): upload the Blobs the Hub lacks, push the Outbox, pull the changes
 * feed, report a heartbeat. Passes run one at a time, so the upload events of one Blob never interleave.
 */
class SyncEngine(
    private val store: SyncStore,
    private val hub: HubClient,
    private val clock: Clock,
    private val appVersion: String,
    private val largeBlobBytes: Long = LARGE_BLOB_BYTES,
) {
    private val passing = ReentrantLock()

    /**
     * [allowLarge]: the network is unmetered, or the user lets large Blobs go over any network. An outcome only the
     * user can mend is kept, and later passes answer with it without asking the Hub until [SyncStore.resume].
     */
    fun run(allowLarge: Boolean): SyncOutcome = passing.withLock {
        store.standing()?.stopped?.let { stopped -> return@withLock STOPS.entries.first { it.value == stopped }.key }
        val outcome = try {
            Pass(allowLarge).run()
        } catch (stop: Stop) {
            stop.outcome
        } catch (trouble: HubTrouble) {
            when (trouble) {
                is HubTrouble.NotReached -> SyncOutcome.HubNotReached
                is HubTrouble.OtherHub -> SyncOutcome.WrongHub
                is HubTrouble.Unreadable -> SyncOutcome.HubFailed
            }
        }
        STOPS[outcome]?.let(store::stop)
        outcome
    }

    private class Stop(val outcome: SyncOutcome) : Exception(outcome.toString())

    private class EpochChanged(val epoch: String) : Exception("epoch changed to $epoch")

    private enum class Step { MOVED, DEFERRED, HUB_FULL }

    private inner class Pass(private val allowLarge: Boolean) {
        private var largeWaiting = false
        private var deferred = false
        private var lastError: String? = null

        /** A commit the Hub answers with missing_blobs sends its Blobs back to waiting (D3); they go up in the same pass. */
        fun run(): SyncOutcome {
            store.forgetSent()
            val standing = store.standing()
            if (standing == null) knowHub() else if (standing.hubId != hub.hubId) return SyncOutcome.WrongHub
            repeat(EPOCH_ROUNDS) {
                try {
                    upload()
                    push()
                    if (store.uploads().any { it.state == TransferState.WAITING }) {
                        upload()
                        push()
                    }
                    pull()
                    heartbeat()
                    return SyncOutcome.Synced(largeWaiting, deferred)
                } catch (changed: EpochChanged) {
                    store.epochChanged(changed.epoch)
                }
            }
            return SyncOutcome.HubFailed
        }

        private fun knowHub() {
            val info = hub.hub().orStop()
            if (info.hubId != hub.hubId) throw Stop(SyncOutcome.WrongHub)
            if (info.minContractVersion > CONTRACT_VERSION) throw Stop(SyncOutcome.ContractTooOld)
            if (!isUuid(info.epoch)) throw Stop(SyncOutcome.HubFailed)
            store.knowHub(info.hubId, info.epoch)
        }

        private fun epoch(): String = checkNotNull(store.standing()).epoch

        /** A Blob sent back to waiting, by a lost session or a copy found intact after a hash_mismatch, goes once more. */
        private fun upload() {
            var round = store.uploads()
            repeat(UPLOAD_ROUNDS) {
                val tried = mutableSetOf<String>()
                for (blob in round) {
                    if (blob.size > largeBlobBytes && !allowLarge) {
                        largeWaiting = true
                        continue
                    }
                    tried += blob.sha256
                    when (upload(blob)) {
                        Step.MOVED -> Unit
                        Step.DEFERRED -> deferred = true
                        Step.HUB_FULL -> {
                            deferred = true
                            lastError = ErrorCode.HUB_STORAGE_FULL.value
                            return
                        }
                    }
                }
                round = store.uploads().filter { it.sha256 in tried && it.state == TransferState.WAITING }
            }
        }

        private fun upload(blob: Upload): Step {
            if (!blob.file.exists()) return missingCopy(blob)
            val session = when (val opened = hub.openUpload(blob.sha256, blob.size)) {
                is Answer.Ok -> when (val value = opened.value) {
                    is HubClient.Opened.Stored -> return moved(blob, BlobEvent.HUB_HAS_BLOB)
                    is HubClient.Opened.Session -> value.session
                }
                is Answer.Refused -> return waits(blob, opened)
            }
            store.onUpload(blob.sha256, BlobEvent.SESSION_OPENED)
            var missing = chunks(session) - session.received.toSet()
            repeat(FINALIZE_ROUNDS) {
                for (index in missing) sendChunk(blob, session.chunkSize, index)?.let { return it }
                when (val finalized = hub.finalize(blob.sha256, blob.size)) {
                    is Answer.Ok -> return moved(blob, BlobEvent.HUB_HAS_BLOB)
                    is Answer.Refused -> when {
                        finalized.code == ErrorCode.INCOMPLETE -> missing = missingChunks(finalized) ?: return waits(blob, finalized)
                        finalized.code == ErrorCode.HASH_MISMATCH -> return copyLost(blob)
                        finalized.status == NOT_FOUND -> return moved(blob, BlobEvent.SESSION_LOST)
                        else -> return waits(blob, finalized)
                    }
                }
            }
            return moved(blob, BlobEvent.SESSION_LOST)
        }

        /** The copy is gone: another device may have uploaded the same bytes, so the Hub is asked first (D2). */
        private fun missingCopy(blob: Upload): Step = when (val finalized = hub.finalize(blob.sha256, blob.size)) {
            is Answer.Ok -> moved(blob, BlobEvent.HUB_HAS_BLOB)
            is Answer.Refused ->
                if (finalized.status == NOT_FOUND || finalized.status == CONFLICT) copyLost(blob) else waits(blob, finalized)
        }

        /** Sends chunk [index]; null when it went, else where the Blob stands now. */
        private fun sendChunk(blob: Upload, chunkSize: Long, index: Long): Step? {
            val bytes = try {
                readChunk(blob.file, chunkSize, index, blob.size) ?: return copyLost(blob)
            } catch (gone: NoSuchFileException) {
                return copyLost(blob)
            } catch (failed: IOException) {
                return moved(blob, BlobEvent.DEFERRED)
            }
            return when (val put = hub.putChunk(blob.sha256, index, bytes)) {
                is Answer.Ok -> null
                is Answer.Refused -> when {
                    put.status == NOT_FOUND -> moved(blob, BlobEvent.SESSION_LOST)
                    put.code == ErrorCode.CHUNK_MISMATCH -> copyLost(blob)
                    else -> waits(blob, put)
                }
            }
        }

        /** The store hashes the copy again before it drops anything; a read of it that fails waits for later. */
        private fun copyLost(blob: Upload): Step = try {
            moved(blob, BlobEvent.COPY_LOST)
        } catch (failed: IOException) {
            moved(blob, BlobEvent.DEFERRED)
        }

        /**
         * A refusal of one Blob's upload leaves that Blob to wait and the pass goes on to the next Blob, the push and
         * the pull. A refusal that speaks for every request still ends the pass, and hub_storage_full ends the uploads.
         */
        private fun waits(blob: Upload, refused: Answer.Refused): Step = when {
            refused.code == ErrorCode.HUB_STORAGE_FULL -> moved(blob, BlobEvent.DEFERRED).let { Step.HUB_FULL }
            refused.status == UNAUTHORIZED || refused.code in PASS_REFUSALS -> throw refusal(refused)
            else -> {
                lastError = "the Hub refused a Blob upload: ${refused.status} ${refused.code?.value.orEmpty()}".trimEnd()
                moved(blob, BlobEvent.DEFERRED)
            }
        }

        private fun moved(blob: Upload, event: BlobEvent): Step {
            store.onUpload(blob.sha256, event)
            return if (event == BlobEvent.DEFERRED) Step.DEFERRED else Step.MOVED
        }

        private fun push() {
            var most = SyncStore.MAX_BATCH
            while (true) {
                val batch = store.nextBatch(most)
                if (batch.isEmpty()) return
                when (val sent = hub.mutations(epoch(), batch.map { it.body })) {
                    is Answer.Ok -> {
                        store.onResults(sent.value.results)
                        if (store.nextBatch(1).firstOrNull()?.mutationId == batch.first().mutationId) return
                    }
                    is Answer.Refused -> {
                        if (sent.status != BAD_REQUEST || sent.code !in BATCH_REFUSALS) throw refusal(sent)
                        if (batch.size == 1) {
                            store.refuse(batch.single().mutationId)
                            lastError = "a Mutation the Hub cannot read was set aside: ${sent.code?.value}"
                            most = SyncStore.MAX_BATCH
                        } else {
                            most = batch.size / 2
                        }
                    }
                }
            }
        }

        private fun pull() {
            store.beginPull()
            while (true) {
                val since = checkNotNull(store.standing()).cursor
                val page = hub.changes(epoch(), since).orStop()
                if (page.hubId != hub.hubId) throw Stop(SyncOutcome.WrongHub)
                store.applyPage(page)
                if (!page.hasMore) return
                if (page.nextSince <= since) throw Stop(SyncOutcome.HubFailed)
            }
        }

        private fun heartbeat() {
            val backlog = store.backlog()
            val status = DeviceStatus(
                appVersion = appVersion,
                pendingUploadCount = backlog.pendingCaptures,
                pendingMutationCount = backlog.pendingMutations,
                rejectedMutationCount = backlog.rejectedMutations,
                lastSuccessfulSyncAt = clock.instant(),
                lastError = lastError,
                cacheBytes = backlog.cacheBytes,
                outboxBytes = backlog.blobBytes,
            )
            hub.status(status).orStop()
        }
    }

    private fun <T> Answer<T>.orStop(): T = when (this) {
        is Answer.Ok -> value
        is Answer.Refused -> throw refusal(this)
    }

    /** What a refusal no operation handles itself means for the whole pass. */
    private fun refusal(refused: Answer.Refused): Exception = when {
        refused.status == CONFLICT && refused.code == ErrorCode.EPOCH_CHANGED ->
            (refused.error?.details?.get("epoch") as? JsonPrimitive)?.takeIf { it.isString }?.content
                ?.takeIf(::isUuid)?.let(::EpochChanged) ?: Stop(SyncOutcome.HubFailed)
        refused.status == UNAUTHORIZED -> Stop(
            when (refused.code) {
                ErrorCode.DEVICE_REVOKED -> SyncOutcome.Revoked
                ErrorCode.DEVICE_DORMANT -> SyncOutcome.Dormant
                else -> SyncOutcome.Unauthorized
            },
        )
        refused.code == ErrorCode.CONTRACT_TOO_OLD -> Stop(SyncOutcome.ContractTooOld)
        else -> Stop(SyncOutcome.HubFailed)
    }

    private companion object {
        const val BAD_REQUEST = 400
        const val UNAUTHORIZED = 401
        const val NOT_FOUND = 404
        const val CONFLICT = 409

        /** A pass follows a 409 epoch_changed this many times before it leaves the rest to the next pass. */
        const val EPOCH_ROUNDS = 3

        /** Finalizes that may answer incomplete before the upload starts over from a new session. */
        const val FINALIZE_ROUNDS = 3

        const val UPLOAD_ROUNDS = 2

        /** What [SyncStore.stop] keeps for each outcome only the user can mend. */
        val STOPS = mapOf(
            SyncOutcome.Unauthorized to ErrorCode.UNAUTHORIZED.value,
            SyncOutcome.Revoked to ErrorCode.DEVICE_REVOKED.value,
            SyncOutcome.WrongHub to "wrong_hub",
            SyncOutcome.ContractTooOld to ErrorCode.CONTRACT_TOO_OLD.value,
        )

        /** Refusals besides 401 that speak for every request of the pass, whichever operation drew them. */
        val PASS_REFUSALS = setOf(ErrorCode.CONTRACT_TOO_OLD, ErrorCode.EPOCH_CHANGED)

        /** The codes of a 400 that refuses a batch for one Mutation in it (openapi/sync.yaml postMutations). */
        val BATCH_REFUSALS = setOf(ErrorCode.INVALID_REQUEST, ErrorCode.NOT_CANONICAL)

        fun chunks(session: UploadSession): List<Long> =
            (0 until (session.size + session.chunkSize - 1) / session.chunkSize).toList()

        fun missingChunks(refused: Answer.Refused): List<Long>? =
            (refused.error?.details?.get("missing_chunks") as? JsonArray)?.map { (it as? JsonPrimitive)?.longOrNull ?: return null }

        /** Bytes [index * chunkSize, min((index + 1) * chunkSize, size)) of [file]; null when the file ends before. */
        fun readChunk(file: File, chunkSize: Long, index: Long, size: Long): ByteArray? {
            val start = index * chunkSize
            val bytes = ByteArray(minOf(chunkSize, size - start).toInt())
            FileChannel.open(file.toPath(), StandardOpenOption.READ).use { channel ->
                val buffer = ByteBuffer.wrap(bytes)
                while (buffer.hasRemaining()) {
                    if (channel.read(buffer, start + buffer.position()) < 0) return null
                }
            }
            return bytes
        }
    }
}
