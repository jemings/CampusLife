package campuslife.android.data

import campuslife.wire.AcceptSuggestion
import campuslife.wire.CommitCapture
import campuslife.wire.CreateItem
import campuslife.wire.CreateRelation
import campuslife.wire.CreateTag
import campuslife.wire.Delete
import campuslife.wire.DismissSuggestion
import campuslife.wire.EntityName
import campuslife.wire.MutableEntity
import campuslife.wire.Mutation
import campuslife.wire.MutationOutcome
import campuslife.wire.MutationResult
import campuslife.wire.MutationSerializer
import campuslife.wire.Patch
import campuslife.wire.Restore
import campuslife.wire.wireJson
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** Queues [mutation] behind every Mutation made before it. */
internal fun CampusLifeDatabase.enqueue(mutation: Mutation, state: TransferState) {
    val body = wireJson.encodeToJsonElement(MutationSerializer, mutation).jsonObject
    val (entity, id) = target(mutation)
    outbox().add(
        OutboxRow(
            mutationId = mutation.mutationId,
            op = body.getValue("op").jsonPrimitive.content,
            entity = entity,
            entityId = id,
            body = body.toString(),
            state = state.column,
        ),
    )
}

/** Moves every held capture.commit whose Blobs are all stored on to pending (decision 6). */
internal fun CampusLifeDatabase.promoteHeldCommits() {
    for (row in outbox().heldWithEveryBlobStored()) {
        outbox().move(row.seq, TransferState.of(row.state).after(OutboxEvent.BLOBS_STORED).column)
    }
}

/** The result that decided a Mutation: for a duplicate, the one its first sending got. */
internal fun MutationResult.decided(): MutationResult =
    if (result == MutationOutcome.DUPLICATE) original ?: this else this

private fun target(mutation: Mutation): Pair<String, String> = when (mutation) {
    is CommitCapture -> EntityName.CAPTURE.value to mutation.capture.id
    is CreateItem -> EntityName.ITEM.value to mutation.id
    is CreateRelation -> EntityName.RELATION.value to mutation.id
    is CreateTag -> EntityName.TAG.value to mutation.id
    is Patch -> mutation.entity.wireName() to mutation.id
    is Delete -> mutation.entity.wireName() to mutation.id
    is Restore -> mutation.entity.wireName() to mutation.id
    is AcceptSuggestion -> EntityName.SUGGESTION.value to mutation.suggestionId
    is DismissSuggestion -> EntityName.SUGGESTION.value to mutation.suggestionId
}

internal fun MutableEntity.wireName(): String =
    wireJson.encodeToJsonElement(MutableEntity.serializer(), this).jsonPrimitive.content
