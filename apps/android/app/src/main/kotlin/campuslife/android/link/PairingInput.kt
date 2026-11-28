package campuslife.android.link

import campuslife.wire.PairingOffer
import campuslife.wire.isUuid
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl

/** What the pairing screen takes from a scanned QR code or from what the user typed (device/pairing_offer). */
object PairingInput {
    /** The offer [text] holds, or null when it is not a device/pairing_offer, as any other QR code is not. */
    fun offer(text: String): PairingOffer? {
        val members = try {
            Json.parseToJsonElement(text) as? JsonObject
        } catch (_: SerializationException) {
            null
        } ?: return null
        val hubUrl = members.text("hub_url")?.takeIf(::isHubUrl) ?: return null
        val hubId = members.text("hub_id")?.takeIf(::isUuid) ?: return null
        val secret = members.text("secret")?.takeIf(SECRET::matches) ?: return null
        return PairingOffer(hubUrl, hubId, secret)
    }

    /** The secret a manual code carries: spaces and '-' dropped, lowercased; null unless 32 hex digits remain. */
    fun secret(code: String): String? =
        code.filterNot { it.isWhitespace() || it == '-' }.lowercase().takeIf(SECRET::matches)

    /** A typed Hub address, with https:// added when left out and a trailing '/' dropped; null unless it is a hub_url. */
    fun hubUrl(text: String): HttpUrl? {
        val typed = text.trim().lowercase().removeSuffix("/")
        val url = if ("://" in typed) typed else "https://$typed"
        return url.takeIf(::isHubUrl)?.toHttpUrl()
    }

    /** The device_name the Hub lists: [name] as one line of at most 64 characters, or "Android" when nothing is left. */
    fun deviceName(name: String?): String {
        val kept = name.orEmpty().trim().codePoints().filter { point -> OFF_LINE.none { point in it } }.limit(NAME_LENGTH).toArray()
        return String(kept, 0, kept.size).trim().ifEmpty { "Android" }
    }

    private fun JsonObject.text(name: String): String? = (get(name) as? JsonPrimitive)?.takeIf { it.isString }?.content

    private fun isHubUrl(text: String) = text.length <= HUB_URL_LENGTH && HUB_URL.matches(text)

    /** device/pairing_offer hub_url. */
    private val HUB_URL = Regex(
        "^https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*" +
            "(:(6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}|[1-5][0-9]{4}|[1-9][0-9]{0,3}))?$",
    )
    private const val HUB_URL_LENGTH = 267

    /** device/pair_request secret. */
    private val SECRET = Regex("^[0-9a-f]{32}$")

    /** What device/pair_request one_line leaves out. */
    private val OFF_LINE = listOf(0x00..0x1f, 0x7f..0x9f, 0x2028..0x202e, 0x2066..0x2069)
    private const val NAME_LENGTH = 64L
}
