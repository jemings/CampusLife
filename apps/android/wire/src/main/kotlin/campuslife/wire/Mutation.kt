package campuslife.wire

import kotlin.reflect.KClass
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.descriptors.buildClassSerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.JsonDecoder
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonEncoder
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** mutation/mutation: one write a client sends (D18). mutation_id is made once and reused on every resend (D4). */
@Serializable(with = MutationSerializer::class)
sealed interface Mutation {
    val mutationId: String
}

@Serializable
data class CommitCapture(
    @SerialName("mutation_id") override val mutationId: String,
    val capture: CaptureCommit,
) : Mutation

@Serializable
data class CreateItem(
    @SerialName("mutation_id") override val mutationId: String,
    val id: String,
    val data: ItemData,
) : Mutation

@Serializable
data class CreateRelation(
    @SerialName("mutation_id") override val mutationId: String,
    val id: String,
    val data: RelationData,
) : Mutation

@Serializable
data class CreateTag(
    @SerialName("mutation_id") override val mutationId: String,
    val id: String,
    val data: TagData,
) : Mutation

/** Per-field compare-and-set: each set key is a column or fields.<key> (A3). */
@Serializable
data class Patch(
    @SerialName("mutation_id") override val mutationId: String,
    val entity: MutableEntity,
    val id: String,
    val set: Map<String, SetEntry>,
) : Mutation

@Serializable
data class Delete(
    @SerialName("mutation_id") override val mutationId: String,
    val entity: MutableEntity,
    val id: String,
) : Mutation

@Serializable
data class Restore(
    @SerialName("mutation_id") override val mutationId: String,
    val entity: MutableEntity,
    val id: String,
) : Mutation

/**
 * Accepts a Suggestion. edits maps a set key to the value written instead of the proposed one;
 * a JsonNull value leaves an optional column out, and a key not in edits keeps the proposal.
 */
@Serializable
data class AcceptSuggestion(
    @SerialName("mutation_id") override val mutationId: String,
    @SerialName("suggestion_id") val suggestionId: String,
    val edits: JsonObject? = null,
) : Mutation

@Serializable
data class DismissSuggestion(
    @SerialName("mutation_id") override val mutationId: String,
    @SerialName("suggestion_id") val suggestionId: String,
) : Mutation

/**
 * Resolves a ConflictRecord (D24 decision 3). Without set it keeps the Hub's values; with set it is a patch of
 * the record's entity, which entity names.
 */
@Serializable
data class ResolveConflict(
    @SerialName("mutation_id") override val mutationId: String,
    @SerialName("conflict_record_id") val conflictRecordId: String,
    val entity: MutableEntity,
    val set: Map<String, SetEntry>? = null,
) : Mutation

/** The entities patch, delete, restore and conflict.resolve name. */
@Serializable
enum class MutableEntity {
    @SerialName("item") ITEM,
    @SerialName("attachment") ATTACHMENT,
    @SerialName("relation") RELATION,
    @SerialName("tag") TAG,
}

/** Writes op, and entity for a create, beside a branch's own members, and picks the branch by them. */
object MutationSerializer : KSerializer<Mutation> {
    private class Branch<T : Mutation>(
        val op: String,
        val entity: String?,
        val type: KClass<T>,
        val serializer: KSerializer<T>,
    ) {
        fun matches(op: String?, entity: String?) = this.op == op && (this.entity == null || this.entity == entity)

        fun encode(mutation: Mutation): JsonObject {
            val tags = listOfNotNull("op" to JsonPrimitive(op), entity?.let { "entity" to JsonPrimitive(it) })
            return JsonObject(tags.toMap() + wireJson.encodeToJsonElement(serializer, type.java.cast(mutation)).jsonObject)
        }
    }

    private val branches = listOf(
        Branch("capture.commit", null, CommitCapture::class, CommitCapture.serializer()),
        Branch("create", "item", CreateItem::class, CreateItem.serializer()),
        Branch("create", "relation", CreateRelation::class, CreateRelation.serializer()),
        Branch("create", "tag", CreateTag::class, CreateTag.serializer()),
        Branch("patch", null, Patch::class, Patch.serializer()),
        Branch("delete", null, Delete::class, Delete.serializer()),
        Branch("restore", null, Restore::class, Restore.serializer()),
        Branch("suggestion.accept", null, AcceptSuggestion::class, AcceptSuggestion.serializer()),
        Branch("suggestion.dismiss", null, DismissSuggestion::class, DismissSuggestion.serializer()),
        Branch("conflict.resolve", null, ResolveConflict::class, ResolveConflict.serializer()),
    )

    override val descriptor: SerialDescriptor = buildClassSerialDescriptor("campuslife.mutation")

    override fun serialize(encoder: Encoder, value: Mutation) {
        val branch = branches.first { it.type.isInstance(value) }
        (encoder as JsonEncoder).encodeJsonElement(branch.encode(value))
    }

    override fun deserialize(decoder: Decoder): Mutation {
        val members = (decoder as JsonDecoder).decodeJsonElement().jsonObject
        val op = members["op"]?.jsonPrimitive?.content
        val entity = members["entity"]?.jsonPrimitive?.content
        val branch = branches.firstOrNull { it.matches(op, entity) }
            ?: throw SerializationException("no Mutation has op $op and entity $entity")
        return decoder.json.decodeFromJsonElement(branch.serializer, members)
    }
}

/** A patch set entry: what the client last saw of the column, and the value to write; to JsonNull removes it. */
@Serializable(with = SetEntrySerializer::class)
data class SetEntry(val seen: Seen, val to: JsonElement)

/** The column as the client last saw it (D18, E11). */
sealed interface Seen {
    /** from: the value, JsonNull when the column was absent. */
    data class Value(val from: JsonElement) : Seen

    /** from_hash: [textHash] of an x-text column, null when the column was absent. */
    data class TextHash(val fromHash: String?) : Seen
}

/** The x-text columns of each entity: a patch sends the from_hash of their text, not the text (E11). */
val textColumns: Map<MutableEntity, Set<String>> = mapOf(MutableEntity.ITEM to setOf("body"))

/** from and from_hash exclude each other, and each may be null, so the entry is read and written as a whole. */
object SetEntrySerializer : KSerializer<SetEntry> {
    override val descriptor: SerialDescriptor = buildClassSerialDescriptor("campuslife.set_entry")

    override fun serialize(encoder: Encoder, value: SetEntry) {
        val from = when (val seen = value.seen) {
            is Seen.Value -> "from" to seen.from
            is Seen.TextHash -> "from_hash" to JsonPrimitive(seen.fromHash)
        }
        (encoder as JsonEncoder).encodeJsonElement(JsonObject(mapOf(from, "to" to value.to)))
    }

    override fun deserialize(decoder: Decoder): SetEntry {
        val members = (decoder as JsonDecoder).decodeJsonElement().jsonObject
        val to = members["to"] ?: throw SerializationException("a set entry has to")
        val fromHash = members["from_hash"]
        val seen = when {
            fromHash is JsonNull -> Seen.TextHash(null)
            fromHash != null -> Seen.TextHash(fromHash.jsonPrimitive.content)
            else -> Seen.Value(members["from"] ?: throw SerializationException("a set entry has from or from_hash"))
        }
        return SetEntry(seen, to)
    }
}

/** mutation/batch_request: body of POST /v1/mutations, one device's Outbox in order. */
@Serializable
data class MutationBatch(val mutations: List<Mutation>)

/** mutation/batch_response: one result per Mutation, in request order. */
@Serializable
data class MutationResults(val results: List<MutationResult>)

/** mutation/result: what the Hub did with one Mutation (D18). Each optional member belongs to one result. */
@Serializable
data class MutationResult(
    @SerialName("mutation_id") val mutationId: String,
    val result: MutationOutcome,
    val reason: RejectReason? = null,
    @SerialName("applied_fields") val appliedFields: List<String>? = null,
    @SerialName("conflicted_fields") val conflictedFields: List<String>? = null,
    @SerialName("conflict_record_ids") val conflictRecordIds: List<String>? = null,
    @SerialName("missing_blobs") val missingBlobs: List<String>? = null,
    val original: MutationResult? = null,
)

@Serializable
enum class MutationOutcome {
    @SerialName("applied") APPLIED,
    @SerialName("duplicate") DUPLICATE,
    @SerialName("conflict") CONFLICT,
    @SerialName("rejected") REJECTED,
    @SerialName("retry") RETRY,
    @SerialName("not_attempted") NOT_ATTEMPTED,
}

@Serializable
enum class RejectReason {
    @SerialName("id_reuse") ID_REUSE,
    @SerialName("not_canonical") NOT_CANONICAL,
    @SerialName("owner") OWNER,
    @SerialName("schema") SCHEMA,
    @SerialName("deleted") DELETED,
    @SerialName("link_target_missing") LINK_TARGET_MISSING,
    @SerialName("unsupported_entity") UNSUPPORTED_ENTITY,
}
