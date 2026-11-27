package campuslife.android.data

import androidx.room.Database
import androidx.room.RoomDatabase

@Database(
    entities = [
        SyncMeta::class,
        CachedRow::class,
        OutboxRow::class,
        BlobRow::class,
        LocalCapture::class,
        LocalAttachment::class,
    ],
    version = 1,
)
abstract class CampusLifeDatabase : RoomDatabase() {
    abstract fun syncMeta(): SyncMetaDao
}
