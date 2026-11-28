package campuslife.android.link

import campuslife.wire.PairingOffer
import okhttp3.HttpUrl

/** What the pairing screen takes from a scanned QR code or from what the user typed (device/pairing_offer). */
object PairingInput {
    /** The offer [text] holds, or null when it is not a device/pairing_offer, as any other QR code is not. */
    fun offer(text: String): PairingOffer? = TODO()

    /** The secret a manual code carries: spaces and '-' dropped, lowercased; null unless 32 hex digits remain. */
    fun secret(code: String): String? = TODO()

    /** A typed Hub address, with https:// added when left out and a trailing '/' dropped; null unless it is a hub_url. */
    fun hubUrl(text: String): HttpUrl? = TODO()

    /** The device_name the Hub lists: [name] as one line of at most 64 characters, or "Android" when nothing is left. */
    fun deviceName(name: String?): String = TODO()
}
