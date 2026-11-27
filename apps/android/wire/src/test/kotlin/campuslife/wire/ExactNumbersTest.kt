package campuslife.wire

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ExactNumbersTest {
    private val credits = "2.50000000000000000001"

    @Test
    fun `a number read from text keeps its digits when a Mutation carrying it is written`() {
        val seen = wireJson.parseToJsonElement("""{"credits":$credits,"slots":[{"n":0.10}]}""").jsonObject
        val patch = Patch(
            mutationId = "0b4c7e2a-5d3f-4e1b-9a6c-8f2d1e3b4a5c",
            entity = MutableEntity.ITEM,
            id = "6a1f3e2d-4b5c-4d6e-8f7a-9b0c1d2e3f4a",
            set = mapOf(
                "fields.credits" to SetEntry(
                    Seen.Value(seen.getValue("credits").withExactNumbers()),
                    seen.getValue("slots").withExactNumbers(),
                ),
            ),
        )

        val text = wireJson.encodeToString(MutationSerializer, patch)

        assertTrue(text, text.contains(""""from":$credits"""))
        assertTrue(text, text.contains(""""to":[{"n":0.10}]"""))
    }

    @Test
    fun `strings, booleans, null and integers come out as they went in`() {
        val text = """{"s":"2.5","b":true,"z":null,"i":9007199254740993,"o":{"a":[false,"x"]}}"""
        val json = wireJson.parseToJsonElement(text)

        assertEquals(text, wireJson.encodeToString(JsonElement.serializer(), json.withExactNumbers()))
    }
}
