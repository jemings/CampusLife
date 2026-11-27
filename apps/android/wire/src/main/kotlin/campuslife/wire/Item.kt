@file:UseSerializers(InstantSerializer::class, DecimalSerializer::class)

package campuslife.wire

import java.math.BigDecimal
import java.time.Instant
import kotlin.reflect.KClass
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.UseSerializers
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject

/**
 * item/item, an Item row of the changes feed. fields stays JSON as the Hub sent it, so members a
 * newer Hub adds survive; [typedFields] reads it for the item's kind.
 */
@Serializable
data class Item(
    val id: String,
    val kind: ItemKind,
    @SerialName("schema_version") val schemaVersion: Long,
    val title: String,
    val body: String? = null,
    @SerialName("starts_at") val startsAt: DateValue? = null,
    @SerialName("due_at") val dueAt: DateValue? = null,
    @SerialName("all_day") val allDay: Boolean? = null,
    val fields: JsonObject,
    val rev: Long,
    @SerialName("deleted_at") val deletedAt: Instant? = null,
    @SerialName("evidence_purged_at") val evidencePurgedAt: Instant? = null,
) {
    fun typedFields(): ItemFields = ItemFields.read(kind, fields)
}

/** The data of a create item Mutation: the Item columns a client writes. */
@Serializable
data class ItemData(
    val kind: ItemKind,
    val title: String,
    val body: String? = null,
    @SerialName("starts_at") val startsAt: DateValue? = null,
    @SerialName("due_at") val dueAt: DateValue? = null,
    @SerialName("all_day") val allDay: Boolean? = null,
    val fields: JsonObject,
) {
    fun typedFields(): ItemFields = ItemFields.read(kind, fields)
}

/** item/fields/<kind>; the fields of a kind this build does not know stay [Unknown]. */
sealed interface ItemFields {
    data class Unknown(val json: JsonObject) : ItemFields

    fun toJson(): JsonObject = when (this) {
        is Unknown -> json
        else -> known.first { it.type.isInstance(this) }.encode(this)
    }

    companion object {
        fun read(kind: ItemKind, fields: JsonObject): ItemFields =
            known.firstOrNull { it.kind == kind }?.decode(fields) ?: Unknown(fields)

        /** The kinds this build reads fields of, one per item/fields/<kind> schema. */
        val knownKinds: List<ItemKind> get() = known.map { it.kind }
    }
}

private class KnownKind<T : ItemFields>(val kind: ItemKind, val type: KClass<T>, val serializer: KSerializer<T>) {
    fun decode(fields: JsonObject): ItemFields = wireJson.decodeFromJsonElement(serializer, fields)

    fun encode(fields: ItemFields): JsonObject =
        wireJson.encodeToJsonElement(serializer, type.java.cast(fields)).jsonObject
}

private val known = listOf(
    KnownKind(ItemKind.TERM, TermFields::class, TermFields.serializer()),
    KnownKind(ItemKind.COURSE, CourseFields::class, CourseFields.serializer()),
    KnownKind(ItemKind.LECTURE_SESSION, LectureSessionFields::class, LectureSessionFields.serializer()),
    KnownKind(ItemKind.ASSIGNMENT, AssignmentFields::class, AssignmentFields.serializer()),
    KnownKind(ItemKind.EXAM, ExamFields::class, ExamFields.serializer()),
    KnownKind(ItemKind.TASK, TaskFields::class, TaskFields.serializer()),
    KnownKind(ItemKind.NOTE, NoteFields::class, NoteFields.serializer()),
    KnownKind(ItemKind.EVENT, EventFields::class, EventFields.serializer()),
    KnownKind(ItemKind.ANNOUNCEMENT, AnnouncementFields::class, AnnouncementFields.serializer()),
    KnownKind(ItemKind.OPPORTUNITY, OpportunityFields::class, OpportunityFields.serializer()),
    KnownKind(ItemKind.ORGANIZATION, OrganizationFields::class, OrganizationFields.serializer()),
)

@Serializable
data class TermFields(
    val year: Int,
    val semester: Semester,
    @SerialName("starts_on") val startsOn: LocalDay,
    @SerialName("ends_on") val endsOn: LocalDay,
) : ItemFields

@Serializable
enum class Semester {
    @SerialName("first") FIRST,
    @SerialName("summer") SUMMER,
    @SerialName("second") SECOND,
    @SerialName("winter") WINTER,
}

@Serializable
data class CourseFields(
    @SerialName("term_id") val termId: String? = null,
    val instructor: String? = null,
    val room: String? = null,
    val credits: BigDecimal? = null,
    @SerialName("schedule_slots") val scheduleSlots: List<LocalTimeRule>? = null,
    @SerialName("default_cloud_policy") val defaultCloudPolicy: CloudPolicy? = null,
    @SerialName("confirm_each_send") val confirmEachSend: Boolean? = null,
) : ItemFields

@Serializable
data class LectureSessionFields(
    @SerialName("course_id") val courseId: String,
    val date: LocalDay,
) : ItemFields

@Serializable
data class AssignmentFields(
    @SerialName("course_id") val courseId: String,
    @SerialName("due_text") val dueText: String? = null,
    val submission: String? = null,
) : ItemFields

@Serializable
data class ExamFields(
    @SerialName("course_id") val courseId: String,
    @SerialName("scope_text") val scopeText: String? = null,
) : ItemFields

@Serializable
data class TaskFields(
    val progress: TaskProgress,
    val reminders: List<Instant>? = null,
) : ItemFields

@Serializable
enum class TaskProgress {
    @SerialName("inbox") INBOX,
    @SerialName("planned") PLANNED,
    @SerialName("in_progress") IN_PROGRESS,
    @SerialName("blocked") BLOCKED,
    @SerialName("completed") COMPLETED,
    @SerialName("cancelled") CANCELLED,
}

@Serializable
data class NoteFields(
    @SerialName("forked_from_artifact") val forkedFromArtifact: String? = null,
) : ItemFields

/** The category of event, announcement, opportunity and organization is an open name (#9 decision 8), so a String. */
@Serializable
data class EventFields(
    val category: String? = null,
    val place: String? = null,
    @SerialName("organization_id") val organizationId: String? = null,
    val url: String? = null,
) : ItemFields

@Serializable
data class AnnouncementFields(
    val category: String? = null,
    @SerialName("organization_id") val organizationId: String? = null,
    val url: String? = null,
) : ItemFields

@Serializable
data class OpportunityFields(
    val category: String? = null,
    @SerialName("eligibility_text") val eligibilityText: String? = null,
    @SerialName("due_text") val dueText: String? = null,
    @SerialName("organization_id") val organizationId: String? = null,
    val url: String? = null,
) : ItemFields

@Serializable
data class OrganizationFields(
    val category: String? = null,
    val url: String? = null,
) : ItemFields
