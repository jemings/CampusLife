package campuslife.android.ui

import campuslife.android.link.DeviceLink
import campuslife.android.link.Linked
import campuslife.android.link.PairingInput
import campuslife.android.sync.HubTrouble
import campuslife.android.sync.Pairing
import java.io.IOException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl

/**
 * What the pairing screen shows, and the work it starts in [scope]. The work belongs to the app, not to the screen:
 * a pairing whose answer is lost leaves a Device the Hub paired that this device never hears of, and its secret spent.
 */
class PairingModel(
    private val link: DeviceLink,
    private val scope: CoroutineScope,
    private val deviceName: String,
) {
    data class State(
        val linked: Linked? = null,
        val busy: Boolean = false,
        val notice: Notice? = null,
        /** Mutations that go to another Hub if the user confirms moving to it; null while no such answer is asked for. */
        val otherHub: Long? = null,
    )

    enum class Notice {
        NOT_AN_OFFER,
        BAD_CODE,
        BAD_ADDRESS,
        SECRET_SENT,
        PAIRED,
        REFUSED,
        NOT_REACHED,
        OTHER_HUB_ANSWERED,
        FAILED,
        REISSUED,
        REISSUE_REFUSED,
        REISSUE_NOT_REACHED,
    }

    private val current = MutableStateFlow(State())
    private val sent = mutableSetOf<String>()
    private var other: DeviceLink.Paired.OtherHub? = null

    val state: StateFlow<State> = current.asStateFlow()

    /** Reads the link kept on this device. */
    fun load() {
        scope.launch {
            val linked = link.linked()
            current.update { it.copy(linked = linked) }
        }
    }

    /** A QR code the camera read; it is read many times a second, and an offer's secret is sent once. */
    fun scanned(text: String) {
        if (waiting()) return
        val offer = PairingInput.offer(text) ?: return note(Notice.NOT_AN_OFFER)
        start(offer.hubUrl.toHttpUrl(), offer.secret, offer.hubId, quiet = true)
    }

    fun typed(hubUrl: String, code: String) {
        val secret = PairingInput.secret(code) ?: return note(Notice.BAD_CODE)
        val url = PairingInput.hubUrl(hubUrl) ?: return note(Notice.BAD_ADDRESS)
        start(url, secret, null, quiet = false)
    }

    fun confirmOtherHub() {
        val chosen = synchronized(sent) {
            other.also {
                other = null
                if (it != null) current.update { state -> state.copy(busy = true, otherHub = null) }
            }
        } ?: return
        finish {
            link.confirm(chosen)
            Notice.PAIRED
        }
    }

    fun cancelOtherHub() {
        synchronized(sent) {
            other = null
            current.update { it.copy(otherHub = null) }
        }
    }

    fun reissue() {
        synchronized(sent) {
            if (waiting()) return
            current.update { it.copy(busy = true, notice = null) }
        }
        finish {
            try {
                when (link.reissue()) {
                    DeviceLink.Reissued.Done -> Notice.REISSUED
                    DeviceLink.Reissued.NotPaired -> Notice.FAILED
                    is DeviceLink.Reissued.Refused -> Notice.REISSUE_REFUSED
                }
            } catch (_: IOException) {
                Notice.REISSUE_NOT_REACHED
            }
        }
    }

    /** Sends [secret] unless it was sent before: the Hub revokes a Device paired with a secret used twice (D23). */
    private fun start(url: HttpUrl, secret: String, expectedHubId: String?, quiet: Boolean) {
        synchronized(sent) {
            if (waiting()) return
            if (!sent.add(secret)) {
                if (!quiet) note(Notice.SECRET_SENT)
                return
            }
            current.update { it.copy(busy = true, notice = null) }
        }
        finish {
            try {
                when (val paired = link.pair(url, secret, expectedHubId, deviceName)) {
                    DeviceLink.Paired.Linked -> Notice.PAIRED
                    is DeviceLink.Paired.OtherHub -> {
                        synchronized(sent) {
                            other = paired
                            current.update { it.copy(otherHub = paired.unsent) }
                        }
                        null
                    }
                }
            } catch (_: Pairing.Refused) {
                Notice.REFUSED
            } catch (_: HubTrouble.OtherHub) {
                Notice.OTHER_HUB_ANSWERED
            } catch (_: HubTrouble.NotReached) {
                Notice.NOT_REACHED
            }
        }
    }

    /** Runs [work] and shows what it ends with; anything it did not expect fails the step, not the app. */
    private fun finish(work: () -> Notice?) {
        scope.launch {
            val notice = try {
                work()
            } catch (_: Exception) {
                Notice.FAILED
            }
            val linked = link.linked()
            current.update { it.copy(linked = linked, busy = false, notice = notice) }
        }
    }

    private fun waiting(): Boolean = current.value.let { it.busy || it.otherHub != null }

    private fun note(notice: Notice) = current.update { it.copy(notice = notice) }
}
