package campuslife.wire

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

class ContractFixturesTest {
    @Test
    fun `the contract fixture root arrives as a system property`() {
        val root = File(
            checkNotNull(System.getProperty("campuslife.contract.fixtures")) {
                "the build sets campuslife.contract.fixtures"
            },
        )
        for (kind in listOf("valid", "forward", "invalid", "policy", "text_hash")) {
            val dir = root.resolve(kind)
            assertTrue("$dir holds at least one file", dir.walk().any { it.isFile })
        }
    }
}
