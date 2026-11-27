package campuslife.android.data

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.PrimaryKey

/** entity_cache: the Hub's last state of one changes-feed row; data is the row JSON as the Hub sent it. */
@Entity(tableName = "entity_cache", primaryKeys = ["entity", "id"])
data class CachedRow(
    val entity: String,
    val id: String,
    val rev: Long,
    val data: String,
)

/** mutation_outbox: one Mutation this device made; body is sent unchanged on every resend (D4). */
@Entity(
    tableName = "mutation_outbox",
    indices = [Index(value = ["mutation_id"], unique = true), Index(value = ["entity", "entity_id"])],
)
data class OutboxRow(
    @PrimaryKey(autoGenerate = true) val seq: Long = 0,
    @ColumnInfo(name = "mutation_id") val mutationId: String,
    val op: String,
    val entity: String,
    @ColumnInfo(name = "entity_id") val entityId: String,
    val body: String,
    val state: String,
    val result: String? = null,
    @ColumnInfo(name = "acked_at_ms") val ackedAtMs: Long? = null,
)

/** blob_transfer: one Blob file this device keeps, and how far its upload got. */
@Entity(tableName = "blob_transfer")
data class BlobRow(
    @PrimaryKey val sha256: String,
    val size: Long,
    val state: String,
)

/** local_capture: a Capture made on this device; draft is its capture/commit without attachments. */
@Entity(tableName = "local_capture")
data class LocalCapture(
    @PrimaryKey val id: String,
    val seal: String,
    val draft: String,
)

/** local_attachment: an Attachment of a local Capture; body is its capture/commit_attachment JSON. */
@Entity(
    tableName = "local_attachment",
    foreignKeys = [
        ForeignKey(
            entity = LocalCapture::class,
            parentColumns = ["id"],
            childColumns = ["capture_id"],
            onDelete = ForeignKey.CASCADE,
        ),
    ],
    indices = [Index(value = ["capture_id", "position"], unique = true), Index(value = ["sha256"])],
)
data class LocalAttachment(
    @PrimaryKey val id: String,
    @ColumnInfo(name = "capture_id") val captureId: String,
    val position: Int,
    val sha256: String,
    val body: String,
)
