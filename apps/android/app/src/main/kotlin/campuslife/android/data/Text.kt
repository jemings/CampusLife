package campuslife.android.data

import java.text.Normalizer
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

internal fun nfc(text: String): String = Normalizer.normalize(text, Normalizer.Form.NFC)

/** The same JSON with every string value in NFC; keys are left as they are. */
internal fun JsonElement.nfcStrings(): JsonElement = when (this) {
    is JsonObject -> JsonObject(mapValues { (_, value) -> value.nfcStrings() })
    is JsonArray -> JsonArray(map { it.nfcStrings() })
    is JsonNull -> this
    is JsonPrimitive -> if (isString) JsonPrimitive(nfc(content)) else this
}
