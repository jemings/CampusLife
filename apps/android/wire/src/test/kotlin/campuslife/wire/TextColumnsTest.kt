package campuslife.wire

import java.io.File
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Test

class TextColumnsTest {
    private val schemas = File(checkNotNull(System.getProperty("campuslife.contract.schemas")) { "the build sets campuslife.contract.schemas" })

    @Test
    fun `the text columns are the x-text members of every patch target`() {
        val mutationDir = schemas.resolve("mutation")
        val targets = read(mutationDir.resolve("mutation.json"))
            .getValue("\$defs").jsonObject.getValue("patch").jsonObject.getValue("x-set-targets").jsonObject

        val fromSchemas = targets.map { (name, target) ->
            val entity = wireJson.decodeFromJsonElement(MutableEntity.serializer(), JsonPrimitive(name))
            val schema = target.jsonObject.getValue("schema").jsonPrimitive.content
            val fields = target.jsonObject["fields"]?.jsonArray?.map { it.jsonPrimitive.content }.orEmpty()
            val columns = textMembers(mutationDir.resolve(schema)) + fields.flatMap { path ->
                textMembers(mutationDir.resolve(path)).map { "fields.$it" }
            }
            entity to columns.toSet()
        }.filter { it.second.isNotEmpty() }.toMap()

        assertEquals(fromSchemas, textColumns)
    }

    private fun textMembers(schema: File): List<String> =
        read(schema)["properties"]?.jsonObject.orEmpty()
            .filter { (_, member) -> (member as? JsonObject)?.get("x-text")?.jsonPrimitive?.boolean == true }
            .keys.toList()

    private fun read(file: File): JsonObject = wireJson.parseToJsonElement(file.canonicalFile.readText()).jsonObject
}
