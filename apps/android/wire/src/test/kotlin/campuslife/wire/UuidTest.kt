package campuslife.wire

import java.io.File
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class UuidTest {
    private val schemas = File(checkNotNull(System.getProperty("campuslife.contract.schemas")) { "the build sets campuslife.contract.schemas" })

    @Test
    fun `UUID_PATTERN is the pattern of common uuid`() {
        val schema = wireJson.parseToJsonElement(schemas.resolve("common/uuid.json").readText()).jsonObject

        assertEquals(schema.getValue("pattern").jsonPrimitive.content, UUID_PATTERN)
    }

    @Test
    fun `isUuid takes a lowercase UUIDv4 and nothing around it`() {
        assertTrue(isUuid("0d9c8b7a-6f5e-4d3c-8b2a-1f0e9d8c7b6a"))
        assertFalse(isUuid("0D9C8B7A-6F5E-4D3C-8B2A-1F0E9D8C7B6A"))
        assertFalse(isUuid("0d9c8b7a-6f5e-1d3c-8b2a-1f0e9d8c7b6a"))
        assertFalse(isUuid("0d9c8b7a-6f5e-4d3c-8b2a-1f0e9d8c7b6a\n"))
        assertFalse(isUuid("not-a-uuid"))
    }
}
