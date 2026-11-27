package campuslife.android.data

import campuslife.wire.ItemData
import campuslife.wire.ItemFields
import campuslife.wire.ItemKind
import campuslife.wire.MutableEntity
import kotlinx.coroutines.flow.Flow
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** One row a patch or delete names. */
data class EntityRef(val entity: MutableEntity, val id: String)

/**
 * The write seam for user data (decision 5): ids, mutation_ids and every set entry's from are made in here.
 * What it shows is the Hub's last state with this device's unfinished Mutations laid over it.
 */
class UserData(private val db: CampusLifeDatabase) {
    /** An Item as shown; [pending] while a Mutation of it has not been answered. */
    data class ItemView(val id: String, val kind: ItemKind, val row: JsonObject, val pending: Boolean) {
        val title: String get() = row.getValue("title").jsonPrimitive.content

        fun typedFields(): ItemFields = ItemFields.read(kind, row["fields"]?.jsonObject ?: JsonObject(emptyMap()))
    }

    /** Creates an Item and returns its id. */
    fun create(data: ItemData): String = TODO()

    /** Sets each key (a column or fields.<key>) to its value over what is shown now; JsonNull removes it. */
    fun patch(ref: EntityRef, changes: Map<String, JsonElement>): Unit = TODO()

    fun delete(ref: EntityRef): Unit = TODO()

    fun observeItems(): Flow<List<ItemView>> = TODO()
}
