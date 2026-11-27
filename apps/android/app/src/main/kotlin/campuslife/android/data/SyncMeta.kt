package campuslife.android.data

import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Entity
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Upsert

/** Where this device stands against its Hub (D3): the Hub's id, the base epoch and the changes-feed cursor. One row. */
@Entity(tableName = "meta")
data class SyncMeta(
    @PrimaryKey val id: Int = 0,
    @ColumnInfo(name = "hub_id") val hubId: String,
    val epoch: String,
    val cursor: Long,
)

@Dao
interface SyncMetaDao {
    @Upsert
    fun put(meta: SyncMeta)

    @Query("SELECT * FROM meta")
    fun get(): SyncMeta?
}
