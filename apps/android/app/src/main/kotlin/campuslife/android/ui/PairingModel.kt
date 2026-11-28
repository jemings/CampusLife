package campuslife.android.ui

import campuslife.android.link.DeviceLink
import campuslife.android.link.Linked
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.StateFlow

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

    val state: StateFlow<State> get() = TODO()

    /** Reads the link kept on this device. */
    fun load(): Unit = TODO()

    /** A QR code the camera read; it is read many times a second, and an offer's secret is sent once. */
    fun scanned(text: String): Unit = TODO()

    fun typed(hubUrl: String, code: String): Unit = TODO()

    fun confirmOtherHub(): Unit = TODO()

    fun cancelOtherHub(): Unit = TODO()

    fun reissue(): Unit = TODO()
}
