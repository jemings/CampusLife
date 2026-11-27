package campuslife.wire

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/** relation/relation: a Relation row; id is from|type|to. Locators stay JSON, the app does not read them. */
@Serializable
data class Relation(
    val id: String,
    val from: String,
    val type: RelationType,
    val to: String,
    val locators: List<JsonObject>? = null,
)

/** The data of a create relation Mutation. */
@Serializable
data class RelationData(val from: String, val type: RelationType, val to: String)

/** tag/tag: a Tag row. */
@Serializable
data class Tag(val id: String, val name: String, val color: String)

/** The data of a create tag Mutation. */
@Serializable
data class TagData(val name: String, val color: String)
