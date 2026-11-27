package campuslife.android.data

import androidx.room.Database
import androidx.room.RoomDatabase

@Database(entities = [SyncMeta::class], version = 1, exportSchema = false)
abstract class CampusLifeDatabase : RoomDatabase() {
    abstract fun syncMeta(): SyncMetaDao
}
