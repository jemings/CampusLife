package campuslife.wire.harness

import campuslife.wire.wireJson
import kotlinx.serialization.KSerializer

/** How the harness reads and writes one schema_id: the model's serializer, then the typed view the app reads. */
class Codec<T>(private val serializer: KSerializer<T>, private val view: (T) -> T = { it }) {
    fun read(text: String): T = view(wireJson.decodeFromString(serializer, text))

    fun roundTrip(text: String): String = wireJson.encodeToString(serializer, read(text))
}

/** Every schema_id with valid or forward fixtures, and the Kotlin model that reads it (#3 decision 3). */
val codecs: Map<String, Codec<*>> = emptyMap()
