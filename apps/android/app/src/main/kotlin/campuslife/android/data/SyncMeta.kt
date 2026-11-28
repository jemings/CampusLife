package campuslife.android.data

import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Entity
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Upsert

/**
 * Where this device stands against its Hub (D3): the Hub's id, the base epoch and the changes-feed cursor. One row.
 * [stopped] says why the sync stopped until the user acts, as a re-pairing or an app update; null while it runs.
 */
@Entity(tableName = "meta")
data class SyncMeta(
    @PrimaryKey val id: Int = 0,
    @ColumnInfo(name = "hub_id") val hubId: String,
    val epoch: String,
    val cursor: Long,
    val stopped: String? = null,
)

@Dao
interface SyncMetaDao {
    @Upsert
    fun put(meta: SyncMeta)

    @Query("SELECT * FROM meta")
    fun get(): SyncMeta?

    @Query("DELETE FROM meta")
    fun clear()

    @Query("UPDATE meta SET cursor = :cursor WHERE id = 0")
    fun setCursor(cursor: Long): Int

    @Query("UPDATE meta SET stopped = :stopped WHERE id = 0")
    fun setStopped(stopped: String?): Int

    /** Takes [epoch] as the base epoch and reads the feed from its start again (D24). */
    @Query("UPDATE meta SET epoch = :epoch, cursor = 0 WHERE id = 0")
    fun restart(epoch: String): Int
}
