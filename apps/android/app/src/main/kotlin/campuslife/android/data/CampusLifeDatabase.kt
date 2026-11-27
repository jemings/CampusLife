package campuslife.android.data

import androidx.room.Database
import androidx.room.RoomDatabase

@Database(entities = [SyncMeta::class], version = 1)
abstract class CampusLifeDatabase : RoomDatabase() {
    abstract fun syncMeta(): SyncMetaDao
}
