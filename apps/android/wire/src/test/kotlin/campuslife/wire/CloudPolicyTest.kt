package campuslife.wire

import org.junit.Assert.assertThrows
import org.junit.Test

class CloudPolicyTest {
    @Test
    fun `a provider call without inputs has no effective policy`() {
        assertThrows(IllegalArgumentException::class.java) { CloudPolicy.effective(emptyList()) }
    }
}
