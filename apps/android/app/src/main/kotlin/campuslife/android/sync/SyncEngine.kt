package campuslife.android.sync

import campuslife.android.data.SyncStore
import java.time.Clock

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
    /** [allowLarge]: the network is unmetered, or the user lets large Blobs go over any network. */
    fun run(allowLarge: Boolean): SyncOutcome = TODO()
}
