package campuslife.android.link

import campuslife.android.data.SyncStore
import campuslife.android.sync.Answer
import campuslife.android.sync.HubClient
import campuslife.android.sync.Pairing
import campuslife.android.sync.SyncEngine
import campuslife.android.sync.SyncOutcome
import campuslife.android.sync.SyncSchedule
import campuslife.wire.ErrorCode
import campuslife.wire.PairRequest
import campuslife.wire.Platform
import okhttp3.HttpUrl

/** The Hub a sync pass goes to and the token it carries. */
data class HubLink(val url: HttpUrl, val hubId: String, val token: String) {
    override fun toString(): String = "HubLink(url=$url, hubId=$hubId)"
}

/** The Hub this device is paired with, as the pairing screen shows it. */
data class Linked(val hubUrl: HttpUrl, val hubId: String, val deviceId: String)

/**
 * This device's link with its Hub: pairing, the token, and the sync passes that use them (D23). A pass and a change
 * of the link run one at a time, so no pass sends with a link other than the one it started with.
 */
class DeviceLink(
    private val vault: PairingVault,
    private val store: SyncStore,
    private val schedule: SyncSchedule,
    private val pairing: Pairing,
    private val client: (HubLink) -> HubClient,
    private val engine: (HubClient) -> SyncEngine,
    private val appVersion: String,
) {
    /** How a pairing ended. */
    sealed interface Paired {
        /** The link is kept and passes run again. */
        data object Linked : Paired

        /**
         * The Hub that answered is not the one the store synced with, and nothing changed: [confirm] moves to it,
         * and [unsent] Mutations go to it. Dropping this leaves the new Hub a Device it never hears from again.
         */
        class OtherHub internal constructor(val unsent: Long, internal val record: PairingVault.Record) : Paired
    }

    /** How a token reissue ended. */
    sealed interface Reissued {
        /** The new token is the one passes carry from now on. */
        data object Done : Reissued

        data object NotPaired : Reissued

        data class Refused(val status: Int, val code: ErrorCode?) : Reissued
    }

    private val lock = Any()
    private var cached: Pair<HubLink, SyncEngine>? = null

    /** The Hub and token the next pass uses; null before pairing. */
    fun current(): HubLink? = vault.read()?.let { HubLink(it.hubUrl, it.hubId, it.latest) }

    fun linked(): Linked? = vault.read()?.let { Linked(it.hubUrl, it.hubId, it.deviceId) }

    /**
     * Pairs with the Hub at [hubUrl] using [secret], once: a second use makes the Hub revoke the Device it paired, so
     * nothing here sends it again (D23). [expectedHubId] is the offer's hub_id when a QR code gave one. Throws
     * [Pairing.Refused] or a [campuslife.android.sync.HubTrouble] when no Device was paired.
     */
    fun pair(hubUrl: HttpUrl, secret: String, expectedHubId: String?, deviceName: String): Paired {
        val paired = pairing.pair(hubUrl, PairRequest(secret, Platform.ANDROID, deviceName, appVersion), expectedHubId)
        val record = PairingVault.Record(hubUrl, paired.hubId, paired.deviceId, paired.token)
        synchronized(lock) {
            val standing = store.standing()
            if (standing != null && standing.hubId != record.hubId) {
                val backlog = store.backlog()
                return Paired.OtherHub(backlog.pendingMutations + backlog.rejectedMutations, record)
            }
            keep(record)
        }
        return Paired.Linked
    }

    /** The user confirmed moving to the other Hub: what waits here goes to it, and the previous Hub's data goes. */
    fun confirm(other: Paired.OtherHub): Unit = synchronized(lock) { keep(other.record) }

    /** Asks the Hub for another token (POST /v1/devices/me/token). Throws a HubTrouble when no answer came. */
    fun reissue(): Reissued {
        val link = current() ?: return Reissued.NotPaired
        return when (val answer = client(link).reissue()) {
            is Answer.Ok -> {
                vault.reissued(link.token, answer.value.token)
                startPasses()
                Reissued.Done
            }
            is Answer.Refused -> Reissued.Refused(answer.status, answer.code)
        }
    }

    /**
     * One sync pass with the latest link. A token the vault has replaced since the pass began is refused once the
     * new one is used, so the pass runs once more with the new one; only a refused latest token is [SyncOutcome.Unauthorized].
     */
    fun sync(allowLarge: Boolean): SyncOutcome = synchronized(lock) {
        val record = vault.read() ?: return SyncOutcome.NotPaired
        settle(record)
        var link = HubLink(record.hubUrl, record.hubId, record.latest)
        var outcome = engineFor(link).run(allowLarge)
        if (outcome == SyncOutcome.Unauthorized) {
            val latest = current()
            if (latest != null && latest.token != link.token) {
                link = latest
                outcome = engineFor(link).run(allowLarge)
            }
        }
        if (outcome is SyncOutcome.Synced) vault.worked(link.token)
        outcome
    }

    /** Holding [lock]. */
    private fun keep(record: PairingVault.Record) {
        vault.write(record)
        settle(record)
        store.resume()
        startPasses()
    }

    /**
     * The vault names a Hub other than the one the store synced with only once the user confirmed moving to it, so
     * the store follows; this also finishes a move whose process died between writing the vault and the store.
     */
    private fun settle(record: PairingVault.Record) {
        val standing = store.standing() ?: return
        if (standing.hubId != record.hubId) store.changeHub()
    }

    /** The user acted: a failed pass may have stopped the passes that run by themselves. */
    private fun startPasses() {
        schedule.keepPeriodic()
        schedule.requestSync()
    }

    private fun engineFor(link: HubLink): SyncEngine =
        cached?.takeIf { it.first == link }?.second ?: engine(client(link)).also { cached = link to it }
}
