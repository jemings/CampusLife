package campuslife.wire.harness

import java.nio.file.Paths
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ContractHarnessTest {
    @get:Rule
    val out = TemporaryFolder()

    @Test
    fun `every fixture decodes, every valid one encodes, and every vector agrees`() {
        val report = runHarness(Paths.get(System.getProperty("campuslife.contract.fixtures")), out.root.toPath())

        assertEquals("", report.problems.joinToString("\n"))
        assertTrue(report.written > 0 && report.forwardRead > 0 && report.vectorsRun > 0)
    }
}
