package campuslife.android.data

import campuslife.android.data.TransferState.ACKED
import campuslife.android.data.TransferState.HELD
import campuslife.android.data.TransferState.PENDING
import campuslife.android.data.TransferState.PULLING
import campuslife.android.data.TransferState.REJECTED
import campuslife.android.data.TransferState.RETRY_WAIT
import campuslife.android.data.TransferState.SETTLED
import campuslife.android.data.TransferState.STORED
import campuslife.android.data.TransferState.UPLOADING
import campuslife.android.data.TransferState.WAITING

/** Where a blob_transfer or mutation_outbox row stands; [column] is its text in the state column. */
enum class TransferState(val column: String) {
    WAITING("waiting"),
    UPLOADING("uploading"),
    RETRY_WAIT("retry_wait"),
    STORED("stored"),
    HELD("held"),
    PENDING("pending"),
    ACKED("acked"),
    PULLING("pulling"),
    SETTLED("settled"),
    REJECTED("rejected"),
    ;

    fun after(event: TransferEvent): TransferState = TRANSITIONS[this to event] ?: this

    companion object {
        fun of(column: String): TransferState = entries.first { it.column == column }
    }
}

sealed interface TransferEvent

/** What an upload call learned about one Blob. */
enum class BlobEvent : TransferEvent {
    SESSION_OPENED,
    HUB_HAS_BLOB,
    SESSION_LOST,
    DEFERRED,
    HUB_LOST_BLOB,
}

/** What a mutation result or a pull means for one outbox row. */
enum class OutboxEvent : TransferEvent {
    APPLIED,
    CONFLICTED,
    REFUSED,
    BLOBS_MISSING,
    BLOBS_GONE,
    BLOBS_STORED,
    PULL_BEGUN,
    PULL_DONE,
}

/** The one table every transfer row moves by; a pair it does not list leaves the row where it is. */
val TRANSITIONS: Map<Pair<TransferState, TransferEvent>, TransferState> = mapOf(
    (WAITING to BlobEvent.SESSION_OPENED) to UPLOADING,
    (RETRY_WAIT to BlobEvent.SESSION_OPENED) to UPLOADING,
    (WAITING to BlobEvent.HUB_HAS_BLOB) to STORED,
    (UPLOADING to BlobEvent.HUB_HAS_BLOB) to STORED,
    (RETRY_WAIT to BlobEvent.HUB_HAS_BLOB) to STORED,
    (UPLOADING to BlobEvent.SESSION_LOST) to WAITING,
    (WAITING to BlobEvent.DEFERRED) to RETRY_WAIT,
    (UPLOADING to BlobEvent.DEFERRED) to RETRY_WAIT,
    (STORED to BlobEvent.HUB_LOST_BLOB) to WAITING,
    (PENDING to OutboxEvent.APPLIED) to ACKED,
    (PENDING to OutboxEvent.CONFLICTED) to SETTLED,
    (PENDING to OutboxEvent.REFUSED) to REJECTED,
    (PENDING to OutboxEvent.BLOBS_MISSING) to HELD,
    (PENDING to OutboxEvent.BLOBS_GONE) to REJECTED,
    (HELD to OutboxEvent.BLOBS_STORED) to PENDING,
    (ACKED to OutboxEvent.PULL_BEGUN) to PULLING,
    (PULLING to OutboxEvent.PULL_DONE) to SETTLED,
)
