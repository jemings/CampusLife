@file:UseSerializers(CalendarDateSerializer::class, WallTimeSerializer::class)

package campuslife.wire

import java.math.BigDecimal
import java.time.Instant
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.format.ResolverStyle
import java.time.temporal.TemporalAccessor
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.UseSerializers
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.PrimitiveSerialDescriptor
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.descriptors.buildClassSerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.JsonDecoder
import kotlinx.serialization.json.JsonEncoder
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonUnquotedLiteral
import kotlinx.serialization.json.jsonPrimitive

/** common/uuid: a lowercase UUIDv4, the form of every entity id and of the Hub's epoch. */
const val UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"

private val uuidRegex = Regex(UUID_PATTERN)

fun isUuid(text: String): Boolean = uuidRegex.matches(text)

/** A java.time value written as exactly one pattern, which is also the only form it reads. */
abstract class PatternSerializer<T : TemporalAccessor>(
    name: String,
    private val format: DateTimeFormatter,
    private val parse: (String, DateTimeFormatter) -> T,
) : KSerializer<T> {
    override val descriptor: SerialDescriptor = PrimitiveSerialDescriptor(name, PrimitiveKind.STRING)

    override fun serialize(encoder: Encoder, value: T) = encoder.encodeString(format.format(value))

    override fun deserialize(decoder: Decoder): T = parse(decoder.decodeString(), format)
}

private fun strict(pattern: String): DateTimeFormatter =
    DateTimeFormatter.ofPattern(pattern).withResolverStyle(ResolverStyle.STRICT)

/** common/instant: UTC with exactly three fraction digits, YYYY-MM-DDTHH:MM:SS.sssZ (D11). */
object InstantSerializer : PatternSerializer<Instant>(
    "campuslife.instant",
    strict("uuuu-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC),
    { text, format -> Instant.from(format.parse(text)) },
)

/** A calendar date YYYY-MM-DD without a zone. */
object CalendarDateSerializer : PatternSerializer<LocalDate>(
    "campuslife.calendar_date",
    strict("uuuu-MM-dd"),
    { text, format -> LocalDate.parse(text, format) },
)

/** A wall-clock time HH:MM, 24-hour. */
object WallTimeSerializer : PatternSerializer<LocalTime>(
    "campuslife.wall_time",
    strict("HH:mm"),
    { text, format -> LocalTime.parse(text, format) },
)

/**
 * A JSON number kept as its exact decimal. It is written as an unquoted literal because a
 * JsonPrimitive of a fraction is encoded through a double, which turns 1.50 into 1.5.
 */
object DecimalSerializer : KSerializer<BigDecimal> {
    override val descriptor: SerialDescriptor = PrimitiveSerialDescriptor("campuslife.decimal", PrimitiveKind.STRING)

    override fun serialize(encoder: Encoder, value: BigDecimal) =
        (encoder as JsonEncoder).encodeJsonElement(JsonUnquotedLiteral(value.toString()))

    override fun deserialize(decoder: Decoder): BigDecimal {
        val primitive = (decoder as JsonDecoder).decodeJsonElement().jsonPrimitive
        if (primitive.isString) throw SerializationException("expected a number, got the string ${primitive.content}")
        return primitive.content.toBigDecimal()
    }
}

/** common/local_date: a day on the calendar of a zone, for all-day dates (D11). */
@Serializable
data class LocalDay(val date: LocalDate, val tz: String)

/** common/local_time_rule: a weekly slot in a zone, from one date to another (D11). */
@Serializable
data class LocalTimeRule(
    /** ISO weekdays, Monday = 1 ... Sunday = 7. */
    val weekdays: List<Int>,
    val start: LocalTime,
    val end: LocalTime,
    val tz: String,
    @SerialName("from_date") val fromDate: LocalDate,
    @SerialName("until_date") val untilDate: LocalDate,
)

/** An Item's starts_at or due_at: an instant, or a [LocalDay] when all_day is true (D11). */
@Serializable(with = DateValueSerializer::class)
sealed interface DateValue {
    data class At(val instant: Instant) : DateValue

    data class On(val day: LocalDay) : DateValue
}

object DateValueSerializer : KSerializer<DateValue> {
    override val descriptor: SerialDescriptor = buildClassSerialDescriptor("campuslife.date_value")

    override fun serialize(encoder: Encoder, value: DateValue) = when (value) {
        is DateValue.At -> encoder.encodeSerializableValue(InstantSerializer, value.instant)
        is DateValue.On -> encoder.encodeSerializableValue(LocalDay.serializer(), value.day)
    }

    override fun deserialize(decoder: Decoder): DateValue {
        val json = decoder as JsonDecoder
        val element = json.decodeJsonElement()
        return if (element is JsonObject) {
            DateValue.On(json.json.decodeFromJsonElement(LocalDay.serializer(), element))
        } else {
            DateValue.At(json.json.decodeFromJsonElement(InstantSerializer, element))
        }
    }
}
