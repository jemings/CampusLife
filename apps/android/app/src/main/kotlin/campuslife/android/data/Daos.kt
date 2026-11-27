package campuslife.android.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Upsert
import kotlinx.coroutines.flow.Flow

@Dao
interface OutboxDao {
    @Insert
    fun add(row: OutboxRow): Long

    @Query("SELECT * FROM mutation_outbox WHERE mutation_id = :mutationId")
    fun byMutationId(mutationId: String): OutboxRow?

    @Query("SELECT * FROM mutation_outbox WHERE state = 'pending' ORDER BY seq LIMIT :max")
    fun pending(max: Int): List<OutboxRow>

    @Query("UPDATE mutation_outbox SET state = :state WHERE seq = :seq")
    fun move(seq: Long, state: String)

    @Query("UPDATE mutation_outbox SET state = :state, result = :result, acked_at_ms = :ackedAtMs WHERE seq = :seq")
    fun end(seq: Long, state: String, result: String, ackedAtMs: Long?)

    @Query(
        """
        SELECT * FROM mutation_outbox WHERE state = 'held' AND NOT EXISTS (
            SELECT 1 FROM local_attachment a LEFT JOIN blob_transfer b ON b.sha256 = a.sha256
            WHERE a.capture_id = mutation_outbox.entity_id AND (b.state IS NULL OR b.state <> 'stored')
        ) ORDER BY seq
        """,
    )
    fun heldWithEveryBlobStored(): List<OutboxRow>

    @Query(
        """
        SELECT * FROM mutation_outbox WHERE state = 'held' AND EXISTS (
            SELECT 1 FROM local_attachment a
            WHERE a.capture_id = mutation_outbox.entity_id
                AND NOT EXISTS (SELECT 1 FROM blob_transfer b WHERE b.sha256 = a.sha256)
        ) ORDER BY seq
        """,
    )
    fun heldWithLostBlob(): List<OutboxRow>

    @Query("SELECT * FROM mutation_outbox WHERE state = :state ORDER BY seq")
    fun inState(state: String): List<OutboxRow>

    @Query(
        """
        SELECT * FROM mutation_outbox WHERE entity = :entity AND entity_id = :entityId
            AND state IN ('held', 'pending', 'acked', 'pulling')
        ORDER BY seq
        """,
    )
    fun overlayOf(entity: String, entityId: String): List<OutboxRow>

    @Query(
        """
        SELECT id, data AS json, 0 AS seq, NULL AS state, NULL AS result FROM entity_cache WHERE entity = 'item'
        UNION ALL
        SELECT entity_id AS id, body AS json, seq, state, result FROM mutation_outbox
            WHERE entity = 'item' AND state IN ('held', 'pending', 'acked', 'pulling')
        ORDER BY seq, id
        """,
    )
    fun observeItemLayers(): Flow<List<ItemLayer>>
}

/** A cached item row (state null) or one of the outbox rows laid over it, in the order they apply. */
data class ItemLayer(val id: String, val json: String, val seq: Long, val state: String?, val result: String?)

@Dao
interface CacheDao {
    @Upsert
    fun put(row: CachedRow)

    @Query("DELETE FROM entity_cache WHERE entity = :entity AND id = :id")
    fun remove(entity: String, id: String)

    @Query("SELECT * FROM entity_cache WHERE entity = :entity AND id = :id")
    fun get(entity: String, id: String): CachedRow?
}

@Dao
interface BlobDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    fun addIfAbsent(row: BlobRow)

    @Query("SELECT * FROM blob_transfer WHERE sha256 = :sha256")
    fun get(sha256: String): BlobRow?

    @Query("UPDATE blob_transfer SET state = :state WHERE sha256 = :sha256")
    fun move(sha256: String, state: String)

    @Query("DELETE FROM blob_transfer WHERE sha256 = :sha256")
    fun remove(sha256: String)

    @Query("SELECT * FROM blob_transfer WHERE state <> 'stored' ORDER BY rowid")
    fun unstored(): List<BlobRow>
}

/** A local Capture with what its transfer state is read from. */
data class CaptureSummary(
    val id: String,
    val seal: String,
    val commitState: String?,
    val retrying: Int,
    val uploading: Int,
    val attachments: Int,
)

@Dao
interface CaptureDao {
    @Insert
    fun add(capture: LocalCapture)

    @Query("SELECT * FROM local_capture WHERE id = :id")
    fun get(id: String): LocalCapture?

    @Query("UPDATE local_capture SET seal = :seal WHERE id = :id")
    fun setSeal(id: String, seal: String)

    @Insert
    fun addAttachment(attachment: LocalAttachment)

    @Query("SELECT * FROM local_attachment WHERE capture_id = :captureId ORDER BY position")
    fun attachments(captureId: String): List<LocalAttachment>

    @Query("SELECT COUNT(*) FROM local_attachment WHERE capture_id = :captureId")
    fun attachmentCount(captureId: String): Int

    /** Whether a Capture still open, or whose capture.commit is held or pending, has a Blob of [sha256]. */
    @Query(
        """
        SELECT EXISTS (
            SELECT 1 FROM local_attachment a JOIN local_capture c ON c.id = a.capture_id
            WHERE a.sha256 = :sha256 AND (c.seal = 'open' OR EXISTS (
                SELECT 1 FROM mutation_outbox o
                WHERE o.entity = 'capture' AND o.entity_id = c.id AND o.state IN ('held', 'pending')
            ))
        )
        """,
    )
    fun stillNeed(sha256: String): Boolean

    @Query(
        """
        SELECT c.id AS id, c.seal AS seal,
            (SELECT o.state FROM mutation_outbox o WHERE o.entity = 'capture' AND o.entity_id = c.id
                ORDER BY o.seq DESC LIMIT 1) AS commitState,
            (SELECT COUNT(*) FROM local_attachment a JOIN blob_transfer b ON b.sha256 = a.sha256
                WHERE a.capture_id = c.id AND b.state = 'retry_wait') AS retrying,
            (SELECT COUNT(*) FROM local_attachment a JOIN blob_transfer b ON b.sha256 = a.sha256
                WHERE a.capture_id = c.id AND b.state = 'uploading') AS uploading,
            (SELECT COUNT(*) FROM local_attachment a WHERE a.capture_id = c.id) AS attachments
        FROM local_capture c ORDER BY c.rowid
        """,
    )
    fun observeSummaries(): Flow<List<CaptureSummary>>
}
