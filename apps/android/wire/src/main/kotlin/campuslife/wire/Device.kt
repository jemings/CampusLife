@file:UseSerializers(InstantSerializer::class)

package campuslife.wire

import java.time.Instant
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.UseSerializers

/** device/platform; a pairing device is android or ipados, windows_hub is the Hub's own row. */
@Serializable
enum class Platform {
    @SerialName("android") ANDROID,
    @SerialName("ipados") IPADOS,
    @SerialName("windows_hub") WINDOWS_HUB,
}

/** device/pairing_offer: what the Hub's QR code holds. */
@Serializable
data class PairingOffer(
    @SerialName("hub_url") val hubUrl: String,
    @SerialName("hub_id") val hubId: String,
    val secret: String,
)

/** device/pair_request: body of POST /v1/pair. */
@Serializable
data class PairRequest(
    val secret: String,
    val platform: Platform,
    @SerialName("device_name") val deviceName: String,
    @SerialName("app_version") val appVersion: String,
)

/** device/pair_response: the answer to a pair and to a token reissue. */
@Serializable
data class PairResponse(
    @SerialName("device_id") val deviceId: String,
    val token: String,
)

/** device/status: the heartbeat a client sends. */
@Serializable
data class DeviceStatus(
    @SerialName("app_version") val appVersion: String,
    @SerialName("pending_upload_count") val pendingUploadCount: Long,
    @SerialName("pending_mutation_count") val pendingMutationCount: Long,
    @SerialName("rejected_mutation_count") val rejectedMutationCount: Long,
    @SerialName("last_successful_sync_at") val lastSuccessfulSyncAt: Instant? = null,
    @SerialName("last_error") val lastError: String? = null,
    @SerialName("retry_after") val retryAfter: Instant? = null,
    @SerialName("cache_bytes") val cacheBytes: Long? = null,
    @SerialName("outbox_bytes") val outboxBytes: Long? = null,
)
