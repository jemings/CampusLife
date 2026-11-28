package campuslife.android.sync

import campuslife.wire.CONTRACT_VERSION
import campuslife.wire.ErrorBody
import campuslife.wire.PairRequest
import campuslife.wire.PairResponse
import campuslife.wire.wireJson
import java.io.IOException
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/** POST /v1/pair, the one call made without a token (D23). It is never sent twice: see [HubHttp.pairingClient]. */
class Pairing(private val http: OkHttpClient = HubHttp.pairingClient()) {
    /** A paired Device: [hubId] is the CampusLife-Hub-Id of the answer, the id every later answer must carry (D3). */
    data class Paired(val hubId: String, val deviceId: String, val token: String)

    /** The Hub refused the secret; [retryAfterSeconds] is set once failed attempts are rate limited. */
    class Refused(val status: Int, val error: ErrorBody?, val retryAfterSeconds: Long?) :
        Exception("pairing refused: $status ${error?.code?.value}")

    /**
     * Pairs with the Hub at [hubUrl], the hub_url of a pairing offer or the address the user typed.
     * [expectedHubId] is the offer's hub_id when the QR code gave one; the answer must come from that Hub.
     */
    fun pair(hubUrl: HttpUrl, request: PairRequest, expectedHubId: String? = null): Paired {
        val call = Request.Builder()
            .url(hubUrl.newBuilder().addPathSegments("v1/pair").build())
            .header(HubClient.CONTRACT, CONTRACT_VERSION.toString())
            .post(wireJson.encodeToString(PairRequest.serializer(), request).toRequestBody("application/json".toMediaType()))
            .build()
        try {
            http.newCall(call).execute().use {
                val hubId = it.header(HubClient.HUB_ID) ?: throw HubTrouble.NotReached("POST /v1/pair: ${it.code} without ${HubClient.HUB_ID}")
                if (expectedHubId != null && hubId != expectedHubId) throw HubTrouble.OtherHub(hubId)
                val text = it.body.string()
                if (!it.isSuccessful) throw Refused(it.code, HubClient.errorOf(text), it.header("Retry-After")?.toLongOrNull())
                val paired = wireJson.decodeFromString(PairResponse.serializer(), text)
                return Paired(hubId, paired.deviceId, paired.token)
            }
        } catch (trouble: HubTrouble) {
            throw trouble
        } catch (failed: IOException) {
            throw HubTrouble.NotReached("POST /v1/pair: ${failed.message}", failed)
        }
    }
}
