package campuslife.wire

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/** common/error: the body of every error response. details is defined per code (incomplete: missing_chunks, epoch_changed: epoch). */
@Serializable
data class ErrorBody(val code: ErrorCode, val message: String, val details: JsonObject? = null)
