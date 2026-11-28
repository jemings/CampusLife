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

/** What the outcome of a pass asks of the sync work WorkManager keeps. */
interface SyncSchedule {
    /** Blobs over [LARGE_BLOB_BYTES] wait: a pass once the device is on an unmetered network (D7). */
    fun requestLarge()

    /** The sync stopped until the user acts: no pass runs by itself until then. */
    fun stopUntilUserActs()
}

/** The WorkManager side of a sync pass: what each outcome means for when WorkManager runs it again. */
class SyncWorker(
    context: Context,
    params: WorkerParameters,
    private val runner: SyncRunner,
    private val schedule: SyncSchedule,
) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val outcome = withContext(Dispatchers.IO) { runner.run(inputData.getBoolean(UNMETERED, false)) }
        val output = workDataOf(OUTCOME to outcome.toString())
        return when (outcome) {
            is SyncOutcome.Synced -> {
                if (outcome.largeWaiting) schedule.requestLarge()
                if (outcome.deferred) Result.retry() else Result.success(output)
            }
            SyncOutcome.NotPaired -> Result.success(output)
            SyncOutcome.HubNotReached, SyncOutcome.HubFailed, SyncOutcome.Dormant -> Result.retry()
            SyncOutcome.Unauthorized, SyncOutcome.Revoked, SyncOutcome.WrongHub, SyncOutcome.ContractTooOld ->
                Result.failure(output)
        }
    }

    companion object {
        const val OUTCOME = "outcome"
        const val UNMETERED = "unmetered"
    }
}

/** Makes [SyncWorker]s for WorkManager; every other worker class is left to the default factory. */
class SyncWorkerFactory(private val runner: () -> SyncRunner, private val schedule: () -> SyncSchedule) : WorkerFactory() {
    override fun createWorker(appContext: Context, workerClassName: String, workerParameters: WorkerParameters): ListenableWorker? =
        if (workerClassName == SyncWorker::class.java.name) {
            SyncWorker(appContext, workerParameters, runner(), schedule())
        } else {
            null
        }
}

/** The sync work WorkManager keeps: a pass on request, one every 15 minutes, and one for large Blobs on Wi-Fi. */
class SyncScheduler(private val work: WorkManager) : SyncSchedule {
    /** A pass soon; queued behind one already running, so a change made during it is not missed. */
    fun requestSync() {
        val request = OneTimeWorkRequestBuilder<SyncWorker>().setConstraints(over(NetworkType.CONNECTED)).build()
        work.enqueueUniqueWork(SYNC, ExistingWorkPolicy.APPEND_OR_REPLACE, request)
    }

    fun keepPeriodic() {
        val request = PeriodicWorkRequestBuilder<SyncWorker>(PERIOD_MINUTES, TimeUnit.MINUTES)
            .setConstraints(over(NetworkType.CONNECTED))
            .build()
        work.enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, request)
    }

    override fun requestLarge() {
        val request = OneTimeWorkRequestBuilder<SyncWorker>()
            .setConstraints(over(NetworkType.UNMETERED))
            .setInputData(workDataOf(SyncWorker.UNMETERED to true))
            .build()
        work.enqueueUniqueWork(LARGE, ExistingWorkPolicy.KEEP, request)
    }

    override fun stopUntilUserActs(): Unit = TODO()

    companion object {
        const val SYNC = "sync"
        const val PERIODIC = "sync-periodic"
        const val LARGE = "sync-large"
        const val PERIOD_MINUTES = 15L

        private fun over(network: NetworkType) = Constraints.Builder().setRequiredNetworkType(network).build()
    }
}
