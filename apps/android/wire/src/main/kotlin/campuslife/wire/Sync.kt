package campuslife.wire

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/** sync/hub: GET /v1/hub (E18). */
@Serializable
data class HubInfo(
    @SerialName("hub_id") val hubId: String,
    val epoch: String,
    @SerialName("contract_version") val contractVersion: Long,
    @SerialName("min_contract_version") val minContractVersion: Long,
)

/** sync/changes_query: the query of GET /v1/changes, as the strings the URL carries. */
@Serializable
data class ChangesQuery(val since: String, val limit: String? = null)

/** sync/changes_page: one page of the changes feed (D3). */
@Serializable
data class ChangesPage(
    @SerialName("hub_id") val hubId: String,
    val epoch: String,
    val changes: List<Change>,
    @SerialName("next_since") val nextSince: Long,
    @SerialName("has_more") val hasMore: Boolean,
)

/**
 * One changes row. data stays JSON as the Hub sent it, so a cache keeps members a newer Hub adds;
 * [row] reads it as the app's model. A tombstone (deleted true) has no data.
 */
@Serializable
data class Change(
    val entity: EntityName,
    val id: String,
    val rev: Long,
    val deleted: Boolean,
    val data: JsonObject? = null,
) {
    fun row(): FeedRow? = data?.let { FeedRow.read(entity, it) }
}

/** A changes row read for the app: the entities it interprets are typed, the rest are kept as JSON. */
sealed interface FeedRow {
    data class OfItem(val item: Item) : FeedRow

    data class OfCapture(val capture: Capture) : FeedRow

    data class OfAttachment(val attachment: Attachment) : FeedRow

    data class Kept(val entity: EntityName, val data: JsonObject) : FeedRow

    companion object {
        fun read(entity: EntityName, data: JsonObject): FeedRow = when (entity) {
            EntityName.ITEM -> OfItem(wireJson.decodeFromJsonElement(Item.serializer(), data))
            EntityName.CAPTURE -> OfCapture(wireJson.decodeFromJsonElement(Capture.serializer(), data))
            EntityName.ATTACHMENT -> OfAttachment(wireJson.decodeFromJsonElement(Attachment.serializer(), data))
            else -> Kept(entity, data)
        }
    }
}
