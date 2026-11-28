package campuslife.android.e2e

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import campuslife.android.data.BlobFiles
import campuslife.android.data.CampusLifeDatabase
import campuslife.android.data.CaptureOutbox
import campuslife.android.data.CaptureOutbox.NewFile
import campuslife.android.data.SyncStore
import campuslife.android.data.TransferState
import campuslife.android.data.UserData
import campuslife.android.sync.Answer
import campuslife.android.sync.HubClient
import campuslife.android.sync.HubHttp
import campuslife.android.sync.Pairing
import campuslife.android.sync.SyncEngine
import campuslife.android.sync.SyncOutcome
import campuslife.wire.AttachmentRole
import campuslife.wire.CloudPolicy
import campuslife.wire.ItemData
import campuslife.wire.ItemKind
import campuslife.wire.MutationOutcome
import campuslife.wire.Origin
import campuslife.wire.OriginType
import campuslife.wire.PairRequest
import campuslife.wire.PairingOffer
import campuslife.wire.Platform
import campuslife.wire.wireJson
import java.io.File
import java.io.IOException
import java.time.Clock
import java.util.Collections
import kotlinx.serialization.json.JsonObject
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType
import okhttp3.RequestBody
import okio.Buffer
import okio.BufferedSink
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith

/**
 * Decision 4: the Kotlin sync engine against a Hub that apps/android/e2e/hub_session.py serves on 127.0.0.1, so the
 * Hub judges every request this client makes. Runs only under that script (app/build.gradle.kts).
 */
@RunWith(AndroidJUnit4::class)
class HubE2eTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val clock = Clock.systemUTC()
    private val url = System.getProperty("campuslife.e2e.url")!!.toHttpUrl()
    private val offer = wireJson.decodeFromString(PairingOffer.serializer(), File(System.getProperty("campuslife.e2e.offer")!!).readText())
    private val photo = NewFile(role = AttachmentRole.PHOTO, cloudPolicy = CloudPolicy.LOCAL_ONLY, declaredMime = "image/jpeg")
    private lateinit var db: CampusLifeDatabase
    private lateinit var captures: CaptureOutbox
    private lateinit var items: UserData
    private lateinit var store: SyncStore

    @Before
    fun open() {
        db = Room.inMemoryDatabaseBuilder(context, CampusLifeDatabase::class.java).allowMainThreadQueries().build()
        val files = BlobFiles(folder.newFolder("blobs"))
        captures = CaptureOutbox(db, files, clock)
        items = UserData(db)
        store = SyncStore(db, files, clock)
    }

    @After
    fun close() = db.close()

    /** One pairing secret pairs once, so this is one walk through the decision 4 steps and then a stale epoch. */
    @Test
    fun `a phone pairs, uploads a capture across a cut mid-chunk, commits it once, reads it back and follows an epoch change`() {
        val paired = Pairing().pair(url, PairRequest(offer.secret, Platform.ANDROID, "e2e phone", APP_VERSION), expectedHubId = offer.hubId)
        val course = items.create(ItemData(kind = ItemKind.COURSE, title = "자료구조", fields = JsonObject(emptyMap())))
        val capture = captures.open(Origin(OriginType.CAMERA))
        captures.addFile(capture, ByteArray(LARGE) { (it % 251).toByte() }.inputStream(), photo)
        captures.addFile(capture, "칠판 사진 한 장".toByteArray().inputStream(), photo)
        captures.seal(capture)
        val (large, small) = store.uploads().map { it.sha256 }
        val puts: MutableList<String> = Collections.synchronizedList(mutableListOf())
        var cut = true
        val http = HubHttp.client().newBuilder().addInterceptor { chain ->
            val request = chain.request()
            if (request.method == "PUT") puts += request.url.encodedPath
            if (cut && request.url.encodedPath == chunk(large, 1)) {
                cut = false
                chain.proceed(request.newBuilder().put(HalfThenCut(request.body!!)).build())
            } else {
                chain.proceed(request)
            }
        }.build()
        val hub = HubClient(url, paired.hubId, paired.token, http)
        val engine = SyncEngine(store, hub, clock, APP_VERSION)

        assertEquals(SyncOutcome.HubNotReached, engine.run(false))
        assertEquals(listOf(chunk(large, 0), chunk(large, 1)), puts)
        puts.clear()
        assertEquals(SyncOutcome.Synced(), engine.run(false))
        assertEquals(listOf(chunk(large, 1), chunk(small, 0)), puts)

        val commit = commitOf(capture)
        assertEquals(TransferState.SETTLED.column, commit.state)
        val epoch = store.standing()!!.epoch
        val resent = (hub.mutations(epoch, listOf(commit.body)) as Answer.Ok).value.results.single()
        assertEquals(MutationOutcome.DUPLICATE, resent.result)
        assertEquals(MutationOutcome.APPLIED, resent.original?.result)
        assertFed(course, capture)

        store.epochChanged(STALE_EPOCH)
        assertEquals(SyncOutcome.Synced(), engine.run(false))
        assertEquals(epoch, store.standing()!!.epoch)
        assertEquals(TransferState.SETTLED.column, commitOf(capture).state)
        assertEquals(listOf(chunk(large, 1), chunk(small, 0)), puts)
        assertFed(course, capture)
    }

    private fun commitOf(captureId: String) =
        db.outbox().inStates(TransferState.entries.map { it.column }).single { it.entityId == captureId }

    private fun assertFed(course: String, capture: String) {
        assertNotNull(db.cache().get("item", course))
        assertNotNull(db.cache().get("capture", capture))
        assertEquals(2, db.cache().rowsOf("attachment").size)
    }

    private fun chunk(sha256: String, index: Int) = "/v1/blobs/uploads/$sha256/chunks/$index"

    /** Sends the first half of [body] to the Hub and then drops the connection, as a phone leaving coverage mid-chunk would. */
    private class HalfThenCut(private val body: RequestBody) : RequestBody() {
        override fun contentType(): MediaType? = body.contentType()

        override fun contentLength(): Long = body.contentLength()

        override fun isOneShot(): Boolean = true

        override fun writeTo(sink: BufferedSink) {
            val bytes = Buffer().also(body::writeTo)
            sink.write(bytes, bytes.size / 2)
            sink.flush()
            throw IOException("connection cut")
        }
    }

    private companion object {
        const val APP_VERSION = "0.1.0"
        const val STALE_EPOCH = "3f2c1b0a-9e8d-4c7b-a6f5-e4d3c2b1a090"

        /** The Hub's 8 MiB chunk and a thousand bytes more: two chunks. */
        const val LARGE = 8 * 1024 * 1024 + 1000
    }
}
