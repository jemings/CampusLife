package campuslife.android.data

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
val TRANSITIONS: Map<Pair<TransferState, TransferEvent>, TransferState> = emptyMap()
