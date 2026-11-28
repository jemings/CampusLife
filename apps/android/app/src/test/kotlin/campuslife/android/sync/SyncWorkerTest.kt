package campuslife.android.sync

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.Configuration
import androidx.work.ListenableWorker
import androidx.work.NetworkType
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.TestListenableWorkerBuilder
import androidx.work.testing.WorkManagerTestInitHelper
import androidx.work.workDataOf
import java.util.Collections
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class SyncWorkerTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val asked: MutableList<Boolean> = Collections.synchronizedList(mutableListOf())
    private val schedule = RecordedSchedule()

    @Test
    fun `a pass that synced succeeds, and one that left Blobs to retry runs again later`() {
        assertEquals("Success", resultOf(SyncOutcome.Synced()))
        assertEquals("Retry", resultOf(SyncOutcome.Synced(deferred = true)))
        assertEquals(RecordedSchedule(), schedule)
    }

    @Test
    fun `a pass that left Blobs for an unmetered network asks for a pass that waits for one`() {
        assertEquals("Success", resultOf(SyncOutcome.Synced(largeWaiting = true)))

        assertEquals(RecordedSchedule(large = 1), schedule)
    }

    @Test
    fun `a pass that did not reach the Hub, or that the Hub failed or held dormant, runs again later`() {
        for (outcome in listOf(SyncOutcome.HubNotReached, SyncOutcome.HubFailed, SyncOutcome.Dormant)) {
            assertEquals("$outcome", "Retry", resultOf(outcome))
        }

        assertEquals(RecordedSchedule(), schedule)
    }

    @Test
    fun `a pass only the user can mend fails and stops the passes that would run by themselves`() {
        val stops = listOf(SyncOutcome.Unauthorized, SyncOutcome.Revoked, SyncOutcome.WrongHub, SyncOutcome.ContractTooOld)

        for (outcome in stops) assertEquals("$outcome", "Failure", resultOf(outcome))

        assertEquals(RecordedSchedule(stops = stops.size), schedule)
    }

    @Test
    fun `a device not paired yet has nothing to sync`() {
        assertEquals("Success", resultOf(SyncOutcome.NotPaired))
    }

    @Test
    fun `the runner hears whether the work waited for an unmetered network`() {
        resultOf(SyncOutcome.Synced())
        resultOf(SyncOutcome.Synced(), unmetered = true)

        assertEquals(listOf(false, true), asked)
    }

    @Test
    fun `the large pass waits for an unmetered network and runs as unmetered`() {
        val work = testWorkManager()

        SyncScheduler(work).requestLarge()

        val info = work.getWorkInfosForUniqueWork(SyncScheduler.LARGE).get().single()
        assertEquals(NetworkType.UNMETERED, info.constraints.requiredNetworkType)
        assertEquals(emptyList<Boolean>(), asked)
        WorkManagerTestInitHelper.getTestDriver(context)!!.setAllConstraintsMet(info.id)
        assertEquals(WorkInfo.State.SUCCEEDED, finished(work, info))
        assertEquals(listOf(true), asked)
    }

    @Test
    fun `a sync asked for during a pass runs after it`() {
        val work = testWorkManager()

        SyncScheduler(work).requestSync()
        SyncScheduler(work).requestSync()

        val chain = work.getWorkInfosForUniqueWork(SyncScheduler.SYNC).get()
        assertEquals(listOf(NetworkType.CONNECTED, NetworkType.CONNECTED), chain.map { it.constraints.requiredNetworkType })
        assertEquals(setOf(WorkInfo.State.ENQUEUED, WorkInfo.State.BLOCKED), chain.map { it.state }.toSet())
    }

    @Test
    fun `stopping until the user acts cancels the periodic and the large pass`() {
        val work = testWorkManager()
        SyncScheduler(work).keepPeriodic()
        SyncScheduler(work).requestLarge()

        SyncScheduler(work).stopUntilUserActs()

        for (name in listOf(SyncScheduler.PERIODIC, SyncScheduler.LARGE)) {
            assertEquals(name, WorkInfo.State.CANCELLED, work.getWorkInfosForUniqueWork(name).get().single().state)
        }
    }

    @Test
    fun `a pass runs every 15 minutes while the device is online, and asking again keeps the one there is`() {
        val work = testWorkManager()

        SyncScheduler(work).keepPeriodic()
        val first = work.getWorkInfosForUniqueWork(SyncScheduler.PERIODIC).get().single()
        SyncScheduler(work).keepPeriodic()

        assertEquals(first.id, work.getWorkInfosForUniqueWork(SyncScheduler.PERIODIC).get().single().id)
        assertEquals(TimeUnit.MINUTES.toMillis(15), first.periodicityInfo?.repeatIntervalMillis)
        assertEquals(NetworkType.CONNECTED, first.constraints.requiredNetworkType)
    }

    private fun factory(outcome: SyncOutcome) = SyncWorkerFactory({ SyncRunner { unmetered -> asked += unmetered; outcome } }, { schedule })

    private fun resultOf(outcome: SyncOutcome, unmetered: Boolean = false): String {
        val worker = TestListenableWorkerBuilder<SyncWorker>(context)
            .setWorkerFactory(factory(outcome))
            .setInputData(workDataOf(SyncWorker.UNMETERED to unmetered))
            .build()
        val result: ListenableWorker.Result = runBlocking { worker.doWork() }
        return result.javaClass.simpleName
    }

    private fun testWorkManager(): WorkManager {
        WorkManagerTestInitHelper.initializeTestWorkManager(
            context,
            Configuration.Builder().setExecutor(SynchronousExecutor()).setWorkerFactory(factory(SyncOutcome.Synced())).build(),
        )
        return WorkManager.getInstance(context)
    }

    private fun finished(work: WorkManager, info: WorkInfo): WorkInfo.State = runBlocking {
        withTimeout(10_000) { work.getWorkInfoByIdFlow(info.id).first { it?.state?.isFinished == true } }!!.state
    }

    private data class RecordedSchedule(var large: Int = 0, var stops: Int = 0) : SyncSchedule {
        override fun requestLarge() {
            large++
        }

        override fun stopUntilUserActs() {
            stops++
        }
    }
}
