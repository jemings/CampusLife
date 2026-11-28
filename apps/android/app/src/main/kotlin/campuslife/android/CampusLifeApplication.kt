package campuslife.android

import android.app.Application
import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.provider.Settings
import androidx.room.Room
import androidx.work.Configuration
import androidx.work.WorkManager
import campuslife.android.data.BlobFiles
import campuslife.android.data.CampusLifeDatabase
import campuslife.android.data.SyncStore
import campuslife.android.link.DeviceLink
import campuslife.android.link.GcmSealer
import campuslife.android.link.KeystoreKey
import campuslife.android.link.PairingInput
import campuslife.android.link.PairingVault
import campuslife.android.sync.HubClient
import campuslife.android.sync.HubHttp
import campuslife.android.sync.Pairing
import campuslife.android.sync.SyncEngine
import campuslife.android.sync.SyncRunner
import campuslife.android.sync.SyncSchedule
import campuslife.android.sync.SyncScheduler
import campuslife.android.sync.SyncWorkerFactory
import campuslife.android.ui.PairingModel
import java.io.File
import java.time.Clock
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

class CampusLifeApplication : Application(), Configuration.Provider {
    val graph: AppGraph by lazy { AppGraph(this) }

    override val workManagerConfiguration: Configuration
        get() = Configuration.Builder()
            .setWorkerFactory(SyncWorkerFactory({ graph.sync }, { graph.schedule }))
            .build()
}

/** The one store of the app and the one link its passes and pairings go through, so passes never overlap. */
class AppGraph(private val context: Context) {
    private val clock: Clock = Clock.systemUTC()
    private val db by lazy { Room.databaseBuilder(context, CampusLifeDatabase::class.java, DATABASE).build() }
    private val files by lazy { BlobFiles(File(context.filesDir, BLOBS)) }
    private val http by lazy { HubHttp.client() }
    private val store by lazy { SyncStore(db, files, clock) }
    private val scheduler by lazy { SyncScheduler(WorkManager.getInstance(context)) }

    /** WorkManager is set up when a schedule first changes, not when the app opens. */
    val schedule: SyncSchedule = object : SyncSchedule {
        override fun requestSync() = scheduler.requestSync()

        override fun keepPeriodic() = scheduler.keepPeriodic()

        override fun requestLarge() = scheduler.requestLarge()

        override fun stopUntilUserActs() = scheduler.stopUntilUserActs()
    }

    /** The vault is kept out of backups: the Keystore key that opens it stays on this device. */
    val link: DeviceLink by lazy {
        DeviceLink(
            PairingVault(File(context.noBackupFilesDir, VAULT), GcmSealer { KeystoreKey.get(VAULT_KEY) }),
            store,
            schedule,
            Pairing(),
            { HubClient(it.url, it.hubId, it.token, http) },
            { SyncEngine(store, it, clock, BuildConfig.VERSION_NAME) },
            BuildConfig.VERSION_NAME,
        )
    }

    val sync: SyncRunner = SyncRunner { unmetered -> link.sync(unmetered || largeAllowed()) }

    val pairing: PairingModel by lazy {
        val name = Settings.Global.getString(context.contentResolver, Settings.Global.DEVICE_NAME) ?: Build.MODEL
        PairingModel(link, CoroutineScope(SupervisorJob() + Dispatchers.IO), PairingInput.deviceName(name))
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
        const val VAULT = "hub-link"
        const val VAULT_KEY = "campuslife.hub-link"
        const val SETTINGS = "sync"

        /** The user's leave for Blobs over 50 MB to go over a metered network (D7); off by default. */
        const val LARGE_BLOBS_ON_METERED = "large_blobs_on_metered"
    }
}
