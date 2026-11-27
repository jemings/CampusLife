package campuslife.wire

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.JsonUnquotedLiteral
import kotlinx.serialization.json.booleanOrNull

/**
 * The same JSON, whose numbers keep their text when a serializer writes it again: kotlinx writes a
 * non-integer number read from text through a Double, so a cached decimal would lose digits on its
 * way back out as a patch's from.
 */
fun JsonElement.withExactNumbers(): JsonElement = when (this) {
    is JsonObject -> JsonObject(mapValues { (_, value) -> value.withExactNumbers() })
    is JsonArray -> JsonArray(map { it.withExactNumbers() })
    is JsonNull -> this
    is JsonPrimitive -> if (isString || booleanOrNull != null) this else JsonUnquotedLiteral(content)
}
