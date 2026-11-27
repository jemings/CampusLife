package campuslife.wire

import java.math.BigDecimal
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Test

class DecimalSerializerTest {
    private val credits = "2.50000000000000000001"
    private val text = """{"credits":$credits}"""

    @Test
    fun `credits keep a decimal a double cannot hold`() {
        val fields = wireJson.decodeFromString(CourseFields.serializer(), text)

        assertEquals(BigDecimal(credits), fields.credits)
        assertEquals(text, wireJson.encodeToString(CourseFields.serializer(), fields))
    }

    @Test
    fun `credits keep their decimal through the typed fields of an Item`() {
        val json = wireJson.parseToJsonElement(text).jsonObject

        assertEquals(text, ItemFields.read(ItemKind.COURSE, json).toJson().toString())
    }
}
