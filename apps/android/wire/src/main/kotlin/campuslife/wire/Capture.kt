@file:UseSerializers(InstantSerializer::class)

package campuslife.wire

import java.time.Instant
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.UseSerializers

/** capture/origin: where a Capture came from. */
@Serializable
data class Origin(
    val type: OriginType,
    val uri: String? = null,
    val app: String? = null,
    val publisher: String? = null,
    @SerialName("distributed_by") val distributedBy: String? = null,
    @SerialName("distributed_at") val distributedAt: Instant? = null,
)

/** capture/timeline: the monotonic-clock timeline of one recording (D15, E15). */
@Serializable
data class Timeline(
    @SerialName("boot_id") val bootId: String,
    val anchors: List<TimelineAnchor>,
    @SerialName("start_uncertainty_ms") val startUncertaintyMs: Long? = null,
    val events: List<TimelineEvent>,
)

/** A wall-clock reading paired with the monotonic clock of the same boot. */
@Serializable
data class TimelineAnchor(
    @SerialName("wall_ms") val wallMs: Long,
    @SerialName("mono_ns") val monoNs: Long,
)

@Serializable
data class TimelineEvent(
    val type: TimelineEventType,
    @SerialName("mono_ns") val monoNs: Long,
    @SerialName("media_pos_ms") val mediaPosMs: Long,
)

@Serializable
enum class TimelineEventType {
    @SerialName("start") START,
    @SerialName("pause") PAUSE,
    @SerialName("resume") RESUME,
    @SerialName("stop") STOP,
    @SerialName("interrupted") INTERRUPTED,
    @SerialName("silenced") SILENCED,
    @SerialName("segment_open") SEGMENT_OPEN,
    @SerialName("segment_close") SEGMENT_CLOSE,
}

@Serializable
enum class CapturedAtPrecision {
    @SerialName("exact") EXACT,
    @SerialName("low") LOW,
}

/** capture/commit: the Capture a client sends in capture.commit, without the Hub's device_id. */
@Serializable
data class CaptureCommit(
    val id: String,
    @SerialName("created_at") val createdAt: Instant,
    val origin: Origin,
    @SerialName("category_hint") val categoryHint: String? = null,
    @SerialName("user_note") val userNote: String? = null,
    val url: String? = null,
    val links: List<CaptureLink>,
    val attachments: List<CommitAttachment>,
    val timeline: Timeline? = null,
)

/** A course or lecture session the user picked while capturing; the Hub makes the Relations (A5). */
@Serializable
data class CaptureLink(val type: CaptureLinkType, val to: String)

@Serializable
enum class CaptureLinkType {
    @SerialName("about") ABOUT,
}

/** capture/commit_attachment: one Attachment of a capture.commit. */
@Serializable
data class CommitAttachment(
    val id: String,
    @SerialName("blob_sha256") val blobSha256: String,
    val role: AttachmentRole,
    val seq: Long? = null,
    @SerialName("original_filename") val originalFilename: String? = null,
    @SerialName("original_filename_raw") val originalFilenameRaw: String? = null,
    @SerialName("declared_mime") val declaredMime: String? = null,
    @SerialName("captured_at") val capturedAt: Instant? = null,
    @SerialName("captured_at_precision") val capturedAtPrecision: CapturedAtPrecision? = null,
    @SerialName("captured_mono_ns") val capturedMonoNs: Long? = null,
    @SerialName("cloud_policy") val cloudPolicy: CloudPolicy,
)

/** capture/capture: a Capture row of the changes feed. */
@Serializable
data class Capture(
    val id: String,
    @SerialName("device_id") val deviceId: String,
    @SerialName("created_at") val createdAt: Instant,
    val origin: Origin,
    @SerialName("category_hint") val categoryHint: String? = null,
    @SerialName("user_note") val userNote: String? = null,
    val url: String? = null,
    val timeline: Timeline? = null,
)

/** capture/attachment: an Attachment row of the changes feed. */
@Serializable
data class Attachment(
    val id: String,
    @SerialName("capture_id") val captureId: String,
    @SerialName("blob_sha256") val blobSha256: String,
    val role: AttachmentRole,
    val seq: Long? = null,
    @SerialName("original_filename") val originalFilename: String? = null,
    @SerialName("original_filename_raw") val originalFilenameRaw: String? = null,
    @SerialName("declared_mime") val declaredMime: String? = null,
    @SerialName("detected_mime") val detectedMime: String? = null,
    @SerialName("captured_at") val capturedAt: Instant? = null,
    @SerialName("captured_at_precision") val capturedAtPrecision: CapturedAtPrecision? = null,
    @SerialName("captured_mono_ns") val capturedMonoNs: Long? = null,
    @SerialName("cloud_policy") val cloudPolicy: CloudPolicy,
    @SerialName("backed_up_at") val backedUpAt: Instant? = null,
)
