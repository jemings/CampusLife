package campuslife.android.sync

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.ListenableWorker
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerFactory
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Runs one sync pass of whatever Hub this device is paired with. */
fun interface SyncRunner {
    /** [unmetered]: the work that runs this pass waited for an unmetered network. */
    fun run(unmetered: Boolean): SyncOutcome
}

/** The WorkManager side of a sync pass: what each outcome means for when WorkManager runs it again. */
class SyncWorker(
    context: Context,
    params: WorkerParameters,
    private val runner: SyncRunner,
    private val onLargeWaiting: () -> Unit,
) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = TODO()

    companion object {
        const val OUTCOME = "outcome"
        const val UNMETERED = "unmetered"
    }
}

/** Makes [SyncWorker]s for WorkManager; every other worker class is left to the default factory. */
class SyncWorkerFactory(private val runner: () -> SyncRunner, private val onLargeWaiting: () -> Unit) : WorkerFactory() {
    override fun createWorker(appContext: Context, workerClassName: String, workerParameters: WorkerParameters): ListenableWorker? =
        if (workerClassName == SyncWorker::class.java.name) {
            SyncWorker(appContext, workerParameters, runner(), onLargeWaiting)
        } else {
            null
        }
}

/** The sync work WorkManager keeps: a pass on request, one every 15 minutes, and one for large Blobs on Wi-Fi. */
class SyncScheduler(private val work: WorkManager) {
    /** A pass soon; queued behind one already running, so a change made during it is not missed. */
    fun requestSync(): Unit = TODO()

    fun keepPeriodic(): Unit = TODO()

    /** A pass once the device is on an unmetered network, for the Blobs over [LARGE_BLOB_BYTES] (D7). */
    fun requestLarge(): Unit = TODO()

    companion object {
        const val SYNC = "sync"
        const val PERIODIC = "sync-periodic"
        const val LARGE = "sync-large"
        const val PERIOD_MINUTES = 15L

        private fun over(network: NetworkType) = Constraints.Builder().setRequiredNetworkType(network).build()
    }
}
