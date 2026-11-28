package campuslife.android.link

import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.GeneralSecurityException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl

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
        val latest: String get() = pendingToken ?: token

        override fun toString(): String = "Record(hubUrl=$hubUrl, hubId=$hubId, deviceId=$deviceId)"
    }

    @Synchronized
    fun read(): Record? {
        if (!file.exists()) return null
        return try {
            decode(Json.parseToJsonElement(sealer.open(file.readBytes()).decodeToString()).jsonObject)
        } catch (_: GeneralSecurityException) {
            null
        } catch (_: IllegalArgumentException) {
            null
        }
    }

    /** Replaces the file whole: a write cut short leaves the record there was. */
    @Synchronized
    fun write(record: Record) {
        val next = File(file.parentFile, "${file.name}.next")
        FileOutputStream(next).use { out ->
            out.write(sealer.seal(encode(record).toString().encodeToByteArray()))
            out.fd.sync()
        }
        Files.move(next.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
    }

    /**
     * POST /v1/devices/me/token, sent with [used], answered [token]. The request went through, so [used] is the token
     * now, and [token] the one to use; one given before and never used no longer works.
     */
    @Synchronized
    fun reissued(used: String, token: String) {
        val record = read() ?: return
        if (used != record.token && used != record.pendingToken) return
        write(record.copy(token = used, pendingToken = token))
    }

    /** A request with [token] went through: if it was the pending token, it is the token and the one before goes. */
    @Synchronized
    fun worked(token: String) {
        val record = read() ?: return
        if (token == record.pendingToken) write(record.copy(token = token, pendingToken = null))
    }

    private fun encode(record: Record): JsonObject = buildJsonObject {
        put(HUB_URL, record.hubUrl.toString())
        put(HUB_ID, record.hubId)
        put(DEVICE_ID, record.deviceId)
        put(TOKEN, record.token)
        record.pendingToken?.let { put(PENDING_TOKEN, it) }
    }

    /** Throws IllegalArgumentException when [members] is not a record this class wrote. */
    private fun decode(members: JsonObject): Record {
        fun text(name: String) = requireNotNull(members[name]) { "no $name" }.jsonPrimitive.content
        return Record(
            hubUrl = text(HUB_URL).toHttpUrl(),
            hubId = text(HUB_ID),
            deviceId = text(DEVICE_ID),
            token = text(TOKEN),
            pendingToken = members[PENDING_TOKEN]?.jsonPrimitive?.content,
        )
    }

    private companion object {
        const val HUB_URL = "hub_url"
        const val HUB_ID = "hub_id"
        const val DEVICE_ID = "device_id"
        const val TOKEN = "token"
        const val PENDING_TOKEN = "pending_token"
    }
}
