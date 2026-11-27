package campuslife.wire

import kotlinx.serialization.json.JsonElement

/** The same JSON, whose numbers keep their text when a serializer writes it again. */
fun JsonElement.withExactNumbers(): JsonElement = this
