package campuslife.android.sync

import campuslife.wire.CONTRACT_VERSION
import campuslife.wire.ChangesPage
import campuslife.wire.DeviceStatus
import campuslife.wire.ErrorBody
import campuslife.wire.ErrorCode
import campuslife.wire.HubInfo
import campuslife.wire.MutationResults
import campuslife.wire.PairResponse
import campuslife.wire.StoredBlob
import campuslife.wire.UploadRequest
import campuslife.wire.UploadSession
import campuslife.wire.wireJson
import java.io.IOException
import java.time.Duration
import kotlinx.serialization.KSerializer
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/** What the Hub answered to one call: the operation's value, or the error it refused with. */
sealed interface Answer<out T> {
    data class Ok<T>(val value: T) : Answer<T>

    /** [error] is null when the body is not a common/error. */
    data class Refused(val status: Int, val error: ErrorBody?) : Answer<Nothing> {
        val code: ErrorCode? get() = error?.code
    }
}

/** A call that brought back no answer of the paired Hub. */
sealed class HubTrouble(message: String, cause: Throwable? = null) : IOException(message, cause) {
    /** No connection, or an answer without CampusLife-Hub-Id, such as the 502 tailscale serve sends while the Hub is down. */
    class NotReached(message: String, cause: Throwable? = null) : HubTrouble(message, cause)

    /** The answer came from a Hub other than the one this device paired with (D3). */
    class OtherHub(val hubId: String) : HubTrouble("answered by Hub $hubId")

    /** A success whose body is not what the operation returns. */
    class Unreadable(message: String, cause: Throwable) : HubTrouble(message, cause)
}

/**
 * The sync operations of openapi/sync.yaml, as the Device paired with Hub [hubId] at [base] calls them with [token].
 * Every request carries CampusLife-Contract; GET /v1/changes and POST /v1/mutations also carry the base epoch (D24).
 */
class HubClient(
    private val base: HttpUrl,
    val hubId: String,
    private val token: String,
    private val http: OkHttpClient,
) {
    /** What opening an upload found: the Blob already stored, or the session to send chunks to. */
    sealed interface Opened {
        data class Stored(val blob: StoredBlob) : Opened

        data class Session(val session: UploadSession) : Opened
    }

    fun hub(): Answer<HubInfo> = send(at("v1/hub").get()) { _, text -> read(HubInfo.serializer(), text) }

    /**
     * POST /v1/devices/me/token: another token for this Device. The one sent keeps working until the new one is first
     * used; asking again gives another and ends the one not used yet (D23).
     */
    fun reissue(): Answer<PairResponse> =
        send(at("v1/devices/me/token").post(ByteArray(0).toRequestBody())) { _, text -> read(PairResponse.serializer(), text) }

    fun openUpload(sha256: String, size: Long): Answer<Opened> =
        send(at("v1/blobs/uploads").post(json(UploadRequest.serializer(), UploadRequest(sha256, size)))) { status, text ->
            if (status == CREATED) {
                Opened.Session(read(UploadSession.serializer(), text))
            } else {
                Opened.Stored(read(StoredBlob.serializer(), text))
            }
        }

    fun putChunk(sha256: String, index: Long, bytes: ByteArray): Answer<Unit> =
        send(at("v1/blobs/uploads/$sha256/chunks/$index").put(bytes.toRequestBody(OCTETS))) { _, _ -> }

    /** The Hub hashes the whole Blob before it answers, so the wait grows with [size]. */
    fun finalize(sha256: String, size: Long): Answer<StoredBlob> =
        send(at("v1/blobs/uploads/$sha256/finalize").post(ByteArray(0).toRequestBody()), finalizeTimeout(size)) { _, text ->
            read(StoredBlob.serializer(), text)
        }

    /** [bodies] are Mutation JSON texts as queued, sent unchanged so a resend is the same Mutation (D4). */
    fun mutations(epoch: String, bodies: List<String>): Answer<MutationResults> {
        val batch = bodies.joinToString(",", prefix = "{\"mutations\":[", postfix = "]}")
        return send(at("v1/mutations").header(EPOCH, epoch).post(batch.toRequestBody(JSON))) { _, text ->
            read(MutationResults.serializer(), text)
        }
    }

    fun changes(epoch: String, since: Long, limit: Int = PAGE_ROWS): Answer<ChangesPage> {
        val url = base.newBuilder()
            .addPathSegments("v1/changes")
            .addQueryParameter("since", since.toString())
            .addQueryParameter("limit", limit.toString())
            .build()
        return send(Request.Builder().url(url).header(EPOCH, epoch).get()) { _, text -> read(ChangesPage.serializer(), text) }
    }

    fun status(status: DeviceStatus): Answer<Unit> =
        send(at("v1/devices/me/status").post(json(DeviceStatus.serializer(), status))) { _, _ -> }

    private fun at(path: String): Request.Builder = Request.Builder().url(base.newBuilder().addPathSegments(path).build())

    private fun <T> send(builder: Request.Builder, readTimeout: Duration? = null, read: (Int, String) -> T): Answer<T> {
        val request = builder
            .header(CONTRACT, CONTRACT_VERSION.toString())
            .header("Authorization", "Bearer $token")
            .build()
        val client = readTimeout?.let { http.newBuilder().readTimeout(it).build() } ?: http
        val what = "${request.method} ${request.url.encodedPath}"
        try {
            client.newCall(request).execute().use { response ->
                val from = response.header(HUB_ID) ?: throw HubTrouble.NotReached("$what: ${response.code} without $HUB_ID")
                if (from != hubId) throw HubTrouble.OtherHub(from)
                val text = response.body.string()
                if (!response.isSuccessful) return Answer.Refused(response.code, errorOf(text))
                return try {
                    Answer.Ok(read(response.code, text))
                } catch (unreadable: IllegalArgumentException) {
                    throw HubTrouble.Unreadable("$what: ${unreadable.message}", unreadable)
                }
            }
        } catch (trouble: HubTrouble) {
            throw trouble
        } catch (failed: IOException) {
            throw HubTrouble.NotReached("$what: ${failed.message}", failed)
        }
    }

    companion object {
        const val CONTRACT = "CampusLife-Contract"
        const val EPOCH = "CampusLife-Epoch"
        const val HUB_ID = "CampusLife-Hub-Id"

        /** The most rows a changes page holds (sync/changes_query). */
        const val PAGE_ROWS = 500

        private const val CREATED = 201
        private val JSON = "application/json".toMediaType()
        private val OCTETS = "application/octet-stream".toMediaType()

        /** A finalize waits half a minute, and a second more for every 10 MB the Hub hashes. */
        fun finalizeTimeout(size: Long): Duration = Duration.ofSeconds(30).plusMillis(size / 10_000)

        /** SerializationException, which a body that is not a common/error throws, is an IllegalArgumentException. */
        internal fun errorOf(text: String): ErrorBody? = try {
            wireJson.decodeFromString(ErrorBody.serializer(), text)
        } catch (unreadable: IllegalArgumentException) {
            null
        }

        private fun <T> read(serializer: KSerializer<T>, text: String): T = wireJson.decodeFromString(serializer, text)

        private fun <T> json(serializer: KSerializer<T>, value: T) =
            wireJson.encodeToString(serializer, value).toRequestBody(JSON)
    }
}
