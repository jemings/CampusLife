package campuslife.wire

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.JsonUnquotedLiteral

/**
 * The same JSON, whose numbers keep their text when a serializer writes it again: kotlinx writes a
 * non-integer number read from text through a Double, so a cached decimal would lose digits on its
 * way back out as a patch's from.
 *
 * @throws IllegalArgumentException for a value the Hub cannot read as JSON: no number of JSON (NaN, an
 *   infinity, TRUE), a fraction or exponent past a double, or an integer of more digits than it reads
 */
fun JsonElement.withExactNumbers(): JsonElement = when (this) {
    is JsonObject -> JsonObject(mapValues { (_, value) -> value.withExactNumbers() })
    is JsonArray -> JsonArray(map { it.withExactNumbers() })
    is JsonNull -> this
    is JsonPrimitive -> if (isString || content == "true" || content == "false") this else JsonUnquotedLiteral(jsonNumber(content))
}

private val JSON_INTEGER = Regex("-?(0|[1-9][0-9]*)")

private val JSON_NUMBER = Regex("-?(0|[1-9][0-9]*)(\\.[0-9]+)?([eE][+-]?[0-9]+)?")

/** The Hub reads an integer as a Python int, which refuses text of more digits than this. */
private const val MAX_INTEGER_DIGITS = 4300

private fun jsonNumber(text: String): String {
    if (JSON_INTEGER.matches(text)) {
        require(text.trimStart('-').length <= MAX_INTEGER_DIGITS) { "${text.take(20)}… has more digits than the Hub reads" }
    } else {
        require(JSON_NUMBER.matches(text)) { "$text is not a JSON number" }
        require(text.toDouble().isFinite()) { "$text is past what a double holds" }
    }
    return text
}
