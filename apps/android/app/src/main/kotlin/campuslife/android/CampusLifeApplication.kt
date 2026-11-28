package campuslife.android

import android.app.Application
import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import androidx.room.Room
import androidx.work.Configuration
import androidx.work.WorkManager
import campuslife.android.data.BlobFiles
import campuslife.android.data.CampusLifeDatabase
import campuslife.android.data.SyncStore
import campuslife.android.sync.HubClient
import campuslife.android.sync.HubHttp
import campuslife.android.sync.SyncEngine
import campuslife.android.sync.SyncOutcome
import campuslife.android.sync.SyncRunner
import campuslife.android.sync.SyncScheduler
import campuslife.android.sync.SyncWorkerFactory
import java.io.File
import java.time.Clock
import okhttp3.HttpUrl

/** The paired Hub as the sync engine reaches it. */
data class HubLink(val url: HttpUrl, val hubId: String, val token: String)

/** Where the paired Hub is read from; null while this device is not paired. */
fun interface HubLinks {
    fun current(): HubLink?
}

class CampusLifeApplication : Application(), Configuration.Provider {
    val graph: AppGraph by lazy { AppGraph(this, HubLinks { null }) }

    override val workManagerConfiguration: Configuration
        get() = Configuration.Builder()
            .setWorkerFactory(SyncWorkerFactory({ graph.sync }, { SyncScheduler(WorkManager.getInstance(this)) }))
            .build()
}

/** The one store of the app, and one sync engine per pairing over it, so passes never overlap. */
class AppGraph(private val context: Context, private val links: HubLinks) {
    private val clock: Clock = Clock.systemUTC()
    private val db by lazy { Room.databaseBuilder(context, CampusLifeDatabase::class.java, DATABASE).build() }
    private val files by lazy { BlobFiles(File(context.filesDir, BLOBS)) }
    private val http by lazy { HubHttp.client() }
    private var engine: Pair<HubLink, SyncEngine>? = null

    val sync: SyncRunner = SyncRunner(::pass)

    @Synchronized
    private fun pass(unmetered: Boolean): SyncOutcome {
        val link = links.current() ?: return SyncOutcome.NotPaired
        val current = engine?.takeIf { it.first == link }?.second
            ?: SyncEngine(SyncStore(db, files, clock), HubClient(link.url, link.hubId, link.token, http), clock, BuildConfig.VERSION_NAME)
                .also { engine = link to it }
        return current.run(unmetered || largeAllowed())
    }

    /** D7: over a metered network only if the user said so. */
    private fun largeAllowed(): Boolean {
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        val capabilities = connectivity.getNetworkCapabilities(connectivity.activeNetwork)
        return capabilities?.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED) == true ||
            context.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).getBoolean(LARGE_BLOBS_ON_METERED, false)
    }

    companion object {
        const val DATABASE = "campuslife.db"
        const val BLOBS = "blobs"
        const val SETTINGS = "sync"

        /** The user's leave for Blobs over 50 MB to go over a metered network (D7); off by default. */
        const val LARGE_BLOBS_ON_METERED = "large_blobs_on_metered"
    }
}
