package campuslife.wire

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** blob/upload_request: opens an upload session for one Blob (D7). */
@Serializable
data class UploadRequest(val sha256: String, val size: Long)

/** blob/upload_session: the chunks the Hub already has; chunk i is bytes [i*chunk_size, min((i+1)*chunk_size, size)). */
@Serializable
data class UploadSession(
    val sha256: String,
    val size: Long,
    @SerialName("chunk_size") val chunkSize: Long,
    val received: List<Long>,
)

/** blob/stored: a finalized Blob. */
@Serializable
data class StoredBlob(val sha256: String, val size: Long)
