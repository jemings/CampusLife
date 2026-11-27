package campuslife.wire.harness

import campuslife.wire.AssignmentFields
import campuslife.wire.Attachment
import campuslife.wire.Capture
import campuslife.wire.CaptureCommit
import campuslife.wire.Change
import campuslife.wire.ChangesPage
import campuslife.wire.ChangesQuery
import campuslife.wire.CloudPolicy
import campuslife.wire.CommitAttachment
import campuslife.wire.CourseFields
import campuslife.wire.CreateItem
import campuslife.wire.DeviceStatus
import campuslife.wire.ErrorBody
import campuslife.wire.ExamFields
import campuslife.wire.FeedRow
import campuslife.wire.HubInfo
import campuslife.wire.InstantSerializer
import campuslife.wire.Item
import campuslife.wire.LectureSessionFields
import campuslife.wire.LocalDay
import campuslife.wire.LocalTimeRule
import campuslife.wire.Mutation
import campuslife.wire.MutationBatch
import campuslife.wire.MutationResult
import campuslife.wire.MutationResults
import campuslife.wire.MutationSerializer
import campuslife.wire.NoteFields
import campuslife.wire.Origin
import campuslife.wire.PairRequest
import campuslife.wire.PairResponse
import campuslife.wire.PairingOffer
import campuslife.wire.Platform
import campuslife.wire.Relation
import campuslife.wire.StoredBlob
import campuslife.wire.Tag
import campuslife.wire.TaskFields
import campuslife.wire.TermFields
import campuslife.wire.Timeline
import campuslife.wire.UploadRequest
import campuslife.wire.UploadSession
import campuslife.wire.wireJson
import kotlinx.serialization.KSerializer
import kotlinx.serialization.builtins.serializer
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject

/** How the harness reads and writes one schema_id: the model's serializer, then the typed view the app reads. */
class Codec<T>(private val serializer: KSerializer<T>, private val view: (T) -> T = { it }) {
    fun read(text: String): T = view(wireJson.decodeFromString(serializer, text))

    fun roundTrip(text: String): String = wireJson.encodeToString(serializer, read(text))
}

/** A shape the app passes along without reading it (#3 decision 3): Artifacts, Suggestions, Locators, ConflictRecords, headers. */
private val kept = Codec(JsonObject.serializer())

private val keptGroups = setOf("artifact", "suggestion", "locator", "conflict_record")

/** The model for a schema_id: a typed one, or [kept] for the shapes decision 3 leaves unread; anything else has none. */
fun codecFor(schemaId: String): Codec<*> =
    typed[schemaId]
        ?: kept.takeIf { schemaId.substringBefore('/') in keptGroups || schemaId.endsWith("_headers") }
        ?: error("no Kotlin model for $schemaId")

/**
 * Every typed schema_id and the Kotlin model that reads it (#3 decision 3). The views put JSON the
 * models keep (Item fields, changes rows) through the typed reading the app uses.
 */
private val typed: Map<String, Codec<*>> = mapOf(
    "blob/stored" to Codec(StoredBlob.serializer()),
    "blob/upload_request" to Codec(UploadRequest.serializer()),
    "blob/upload_session" to Codec(UploadSession.serializer()),
    "capture/attachment" to Codec(Attachment.serializer()),
    "capture/capture" to Codec(Capture.serializer()),
    "capture/commit" to Codec(CaptureCommit.serializer()),
    "capture/commit_attachment" to Codec(CommitAttachment.serializer()),
    "capture/origin" to Codec(Origin.serializer()),
    "capture/timeline" to Codec(Timeline.serializer()),
    "common/box" to Codec(JsonArray.serializer()),
    "common/cloud_policy" to Codec(CloudPolicy.serializer()),
    "common/entity_ref" to Codec(String.serializer()),
    "common/error" to Codec(ErrorBody.serializer()),
    "common/instant" to Codec(InstantSerializer),
    "common/int64" to Codec(Long.serializer()),
    "common/local_date" to Codec(LocalDay.serializer()),
    "common/local_time_rule" to Codec(LocalTimeRule.serializer()),
    "common/sha256" to Codec(String.serializer()),
    "common/tz" to Codec(String.serializer()),
    "common/uuid" to Codec(String.serializer()),
    "device/pair_request" to Codec(PairRequest.serializer()),
    "device/pair_response" to Codec(PairResponse.serializer()),
    "device/pairing_offer" to Codec(PairingOffer.serializer()),
    "device/platform" to Codec(Platform.serializer()),
    "device/status" to Codec(DeviceStatus.serializer()),
    "item/fields/assignment" to Codec(AssignmentFields.serializer()),
    "item/fields/course" to Codec(CourseFields.serializer()),
    "item/fields/exam" to Codec(ExamFields.serializer()),
    "item/fields/lecture_session" to Codec(LectureSessionFields.serializer()),
    "item/fields/note" to Codec(NoteFields.serializer()),
    "item/fields/task" to Codec(TaskFields.serializer()),
    "item/fields/term" to Codec(TermFields.serializer()),
    "item/item" to Codec(Item.serializer(), ::typedItem),
    "mutation/batch_request" to Codec(MutationBatch.serializer()) { it.copy(mutations = it.mutations.map(::typedMutation)) },
    "mutation/batch_response" to Codec(MutationResults.serializer()),
    "mutation/mutation" to Codec(MutationSerializer, ::typedMutation),
    "mutation/result" to Codec(MutationResult.serializer()),
    "relation/relation" to Codec(Relation.serializer()),
    "sync/changes_page" to Codec(ChangesPage.serializer()) { it.copy(changes = it.changes.map(::typedChange)) },
    "sync/changes_query" to Codec(ChangesQuery.serializer()),
    "sync/hub" to Codec(HubInfo.serializer()),
    "tag/tag" to Codec(Tag.serializer()),
)

private fun typedItem(item: Item): Item = item.copy(fields = item.typedFields().toJson())

private fun typedMutation(mutation: Mutation): Mutation = when (mutation) {
    is CreateItem -> mutation.copy(data = mutation.data.copy(fields = mutation.data.typedFields().toJson()))
    else -> mutation
}

private fun typedChange(change: Change): Change = change.copy(
    data = when (val row = change.row()) {
        null -> null
        is FeedRow.OfItem -> wireJson.encodeToJsonElement(Item.serializer(), typedItem(row.item)).jsonObject
        is FeedRow.OfCapture -> wireJson.encodeToJsonElement(Capture.serializer(), row.capture).jsonObject
        is FeedRow.OfAttachment -> wireJson.encodeToJsonElement(Attachment.serializer(), row.attachment).jsonObject
        is FeedRow.Kept -> row.data
    },
)
