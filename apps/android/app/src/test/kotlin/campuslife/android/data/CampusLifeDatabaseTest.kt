package campuslife.android.data

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class CampusLifeDatabaseTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()

    @Test
    fun `sync meta survives closing and reopening the database`() {
        val meta = SyncMeta(
            cursor = 42,
            hubId = "c56a4180-65aa-42ec-a945-5fd21dec0538",
            epoch = "e4eaaaf2-d142-41ff-9e1f-2b3c4d5e6f70",
        )

        val first = open()
        first.syncMeta().put(meta)
        first.close()

        val second = open()
        try {
            assertEquals(meta, second.syncMeta().get())
        } finally {
            second.close()
        }
    }

    private fun open(): CampusLifeDatabase =
        Room.databaseBuilder(context, CampusLifeDatabase::class.java, "campuslife-test.db")
            .allowMainThreadQueries()
            .build()
}
