package campuslife.android

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.Configuration
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class WorkManagerTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()

    @Test
    fun `an enqueued worker runs to success`() {
        WorkManagerTestInitHelper.initializeTestWorkManager(
            context,
            Configuration.Builder().setExecutor(SynchronousExecutor()).build(),
        )
        val workManager = WorkManager.getInstance(context)
        val request = OneTimeWorkRequestBuilder<SucceedingWorker>().build()

        workManager.enqueue(request).result.get()

        assertEquals(WorkInfo.State.SUCCEEDED, workManager.getWorkInfoById(request.id).get()?.state)
    }

    class SucceedingWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
        override fun doWork(): Result = Result.success()
    }
}
