package campuslife.android.data

import androidx.room.Database
import androidx.room.RoomDatabase
import java.util.concurrent.Callable

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

    abstract fun outbox(): OutboxDao

    abstract fun blobs(): BlobDao

    abstract fun captures(): CaptureDao
}

internal fun <T> CampusLifeDatabase.inTransaction(body: () -> T): T = runInTransaction(Callable { body() })
