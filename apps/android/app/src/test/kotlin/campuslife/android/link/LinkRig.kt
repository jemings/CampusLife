package campuslife.android.link

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import campuslife.android.data.BlobFiles
import campuslife.android.data.CampusLifeDatabase
import campuslife.android.data.CaptureOutbox
import campuslife.android.data.SyncStore
import campuslife.android.data.UserData
import campuslife.android.sync.FakeHub
import campuslife.android.sync.HubClient
import campuslife.android.sync.Pairing
import campuslife.android.sync.RecordedSchedule
import campuslife.android.sync.SyncEngine
import java.io.File
import java.time.Clock
import java.time.Instant
import java.time.ZoneOffset
import javax.crypto.KeyGenerator
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient

/** A [DeviceLink] over an in-memory store and a vault in [dir], with a fake Hub at [FIRST] and another at [SECOND]. */
class LinkRig(dir: File) : AutoCloseable {
    val clock: Clock = Clock.fixed(Instant.parse("2026-03-02T01:00:00.123Z"), ZoneOffset.UTC)
    val first = FakeHub()
    val second = FakeHub(hubId = SECOND_HUB_ID, epoch = SECOND_EPOCH).apply { token = SECOND_TOKEN }
    val schedule = RecordedSchedule()
    val db: CampusLifeDatabase = Room.inMemoryDatabaseBuilder(ApplicationProvider.getApplicationContext<Context>(), CampusLifeDatabase::class.java)
        .allowMainThreadQueries()
        .build()
    private val files = BlobFiles(File(dir, "blobs").apply { mkdirs() })
    val captures = CaptureOutbox(db, files, clock)
    val items = UserData(db)
    val store = SyncStore(db, files, clock)
    private val key = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
    val vault = PairingVault(File(dir, "link"), GcmSealer { key })
    private val http = OkHttpClient.Builder()
        .addInterceptor { chain -> (if (chain.request().url.host == SECOND.host) second else first).intercept(chain) }
        .build()
    val link = DeviceLink(
        vault,
        store,
        schedule,
        Pairing(http),
        { HubClient(it.url, it.hubId, it.token, http) },
        { SyncEngine(store, it, clock, VERSION) },
        VERSION,
    )

    override fun close() = db.close()

    companion object {
        const val VERSION = "0.1.0"
        const val SECOND_HUB_ID = "9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a"
        const val SECOND_EPOCH = "1e2d3c4b-5a69-4788-9a0b-c1d2e3f4a5b6"
        const val SECOND_TOKEN = "fake-token-second"
        val FIRST = "https://first.example.invalid/".toHttpUrl()
        val SECOND = "https://second.example.invalid/".toHttpUrl()
        val FIRST_RECORD = PairingVault.Record(FIRST, FakeHub.HUB_ID, FakeHub.DEVICE_ID, FakeHub.TOKEN)
        val SECOND_RECORD = PairingVault.Record(SECOND, SECOND_HUB_ID, FakeHub.DEVICE_ID, SECOND_TOKEN)
    }
}
