package campuslife.wire

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

class ContractFixturesTest {
    @Test
    fun `the build hands the tests the contract package's fixture tree`() {
        val root = File(
            checkNotNull(System.getProperty("campuslife.contract.fixtures")) {
                "the build sets campuslife.contract.fixtures"
            },
        )
        for (kind in listOf("valid", "forward", "invalid", "policy", "text_hash")) {
            assertTrue("$root/$kind is a directory", root.resolve(kind).isDirectory)
        }
    }
}
