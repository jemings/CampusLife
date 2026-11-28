package campuslife.android.link

import java.io.File
import okhttp3.HttpUrl

/**
 * The link this device keeps with its Hub, in one file sealed by [sealer] (decision 12). A file the key no longer
 * opens, as after the app's files came to another device, reads as no link: the device pairs again.
 */
class PairingVault(private val file: File, private val sealer: Sealer) {
    /**
     * [pendingToken] is the last token POST /v1/devices/me/token gave; [token] is kept until a request with it goes
     * through, which ends [token] on the Hub (D23).
     */
    data class Record(
        val hubUrl: HttpUrl,
        val hubId: String,
        val deviceId: String,
        val token: String,
        val pendingToken: String? = null,
    ) {
        /** The token every request carries: the last one the Hub gave. */
        val latest: String get() = TODO()

        override fun toString(): String = "Record(hubUrl=$hubUrl, hubId=$hubId, deviceId=$deviceId)"
    }

    fun read(): Record? = TODO()

    fun write(record: Record): Unit = TODO()

    /**
     * POST /v1/devices/me/token, sent with [used], answered [token]. The request went through, so [used] is the token
     * now, and [token] the one to use; one given before and never used no longer works.
     */
    fun reissued(used: String, token: String): Unit = TODO()

    /** A request with [token] went through: if it was the pending token, it is the token and the one before goes. */
    fun worked(token: String): Unit = TODO()
}
