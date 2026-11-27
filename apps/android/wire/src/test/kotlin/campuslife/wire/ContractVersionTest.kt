package campuslife.wire

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Test

class ContractVersionTest {
    @Test
    fun `the client speaks the CONTRACT_VERSION of the contract package`() {
        val init = File(System.getProperty("campuslife.contract.init")).readText()
        val declared = Regex("""^CONTRACT_VERSION = (\d+)$""", RegexOption.MULTILINE)
            .findAll(init)
            .map { it.groupValues[1].toLong() }
            .toList()

        assertEquals(listOf(CONTRACT_VERSION), declared)
    }
}
