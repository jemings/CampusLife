package campuslife.android.data

import campuslife.wire.CreateItem
import campuslife.wire.Delete
import campuslife.wire.ItemData
import campuslife.wire.ItemFields
import campuslife.wire.ItemKind
import campuslife.wire.MutableEntity
import campuslife.wire.MutationResult
import campuslife.wire.MutationSerializer
import campuslife.wire.Patch
import campuslife.wire.Restore
import campuslife.wire.Seen
import campuslife.wire.SetEntry
import campuslife.wire.textColumns
import campuslife.wire.textHash
import campuslife.wire.withExactNumbers
import campuslife.wire.wireJson
import java.util.UUID
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
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
    fun create(data: ItemData): String {
        require(data.title.isNotEmpty()) { "an Item has a title" }
        val id = UUID.randomUUID().toString()
        val canonical = data.copy(
            title = nfc(data.title),
            body = data.body?.let { nfc(it) },
            fields = data.fields.canonical().jsonObject,
        )
        db.inTransaction { db.enqueue(CreateItem(UUID.randomUUID().toString(), id, canonical), TransferState.PENDING) }
        return id
    }

    /** Sets each key (a column or fields.<key>) to its value over what is shown now; JsonNull removes it. */
    fun patch(ref: EntityRef, changes: Map<String, JsonElement>): Unit = db.inTransaction {
        for (key in changes.keys) require(SET_KEY.matches(key) && key != FIELDS) { "$key is not a set key" }
        val shown = requireShown(ref).row
        val hashed = textColumns[ref.entity].orEmpty()
        val set = changes
            .mapValues { (_, to) -> to.canonical() }
            .filter { (key, to) -> valueAt(shown, key) != to }
            .mapValues { (key, to) ->
                val from = valueAt(shown, key)
                val seen = if (key in hashed) Seen.TextHash(textOf(from)?.let(::textHash)) else Seen.Value(from.withExactNumbers())
                SetEntry(seen, to)
            }
        if (set.isEmpty()) return@inTransaction
        db.enqueue(Patch(UUID.randomUUID().toString(), ref.entity, ref.id, set), TransferState.PENDING)
    }

    fun delete(ref: EntityRef): Unit = db.inTransaction {
        requireShown(ref)
        db.enqueue(Delete(UUID.randomUUID().toString(), ref.entity, ref.id), TransferState.PENDING)
    }

    /** Items as shown, soft-deleted ones left out. */
    fun observeItems(): Flow<List<ItemView>> = db.outbox().observeItemLayers().map { layers ->
        layers.groupBy { it.id }.mapNotNull { (id, rows) ->
            val base = rows.firstOrNull { it.state == null }?.json
            val shown = fold(base, rows.mapNotNull { row -> row.state?.let { Layer(row.json, it, row.result) } })
            shown?.takeUnless { it.deleted }?.let { ItemView(id, ItemKind(it.row.getValue("kind").jsonPrimitive.content), it.row, it.pending) }
        }
    }

    /** One outbox row laid over the cache; [result] is the answer that ended its sending, if any. */
    private class Layer(val body: String, val state: String, val result: String?) {
        /** The keys of a partly applied patch the Hub kept its own value for: the cache shows those. */
        fun conflicted(): List<String> =
            result?.let { wireJson.decodeFromString(MutationResult.serializer(), it).decided().conflictedFields }.orEmpty()
    }

    private class Shown(val row: JsonObject, val deleted: Boolean, val pending: Boolean)

    private fun requireShown(ref: EntityRef): Shown {
        val entity = ref.entity.wireName()
        val base = db.cache().get(entity, ref.id)?.data
        val shown = fold(base, db.outbox().overlayOf(entity, ref.id).map { Layer(it.body, it.state, it.result) })
        return requireNotNull(shown?.takeUnless { it.deleted }) { "$entity ${ref.id} is not shown" }
    }

    private fun fold(base: String?, layers: List<Layer>): Shown? {
        var row = base?.let { wireJson.parseToJsonElement(it).jsonObject }
        var deleted = row?.get(DELETED_AT).let { it != null && it != JsonNull }
        var pending = false
        for (layer in layers) {
            when (val mutation = wireJson.decodeFromString(MutationSerializer, layer.body)) {
                is CreateItem -> {
                    row = created(mutation)
                    deleted = false
                }
                is Patch -> row = row?.let { overlay(it, mutation.set - layer.conflicted().toSet()) }
                is Delete -> deleted = true
                is Restore -> deleted = false
                else -> Unit
            }
            if (layer.state in UNANSWERED) pending = true
        }
        return row?.let { Shown(it, deleted, pending) }
    }

    private fun created(create: CreateItem): JsonObject {
        val data = create.data.copy(fields = create.data.fields.withExactNumbers().jsonObject)
        return JsonObject(mapOf(ID to JsonPrimitive(create.id)) + wireJson.encodeToJsonElement(ItemData.serializer(), data).jsonObject)
    }

    private fun overlay(row: JsonObject, set: Map<String, SetEntry>): JsonObject =
        set.entries.fold(row) { shown, (key, entry) ->
            val field = key.removePrefix(FIELDS_PREFIX)
            if (field == key) {
                shown.with(key, entry.to)
            } else {
                shown.with(FIELDS, (shown[FIELDS] as? JsonObject ?: JsonObject(emptyMap())).with(field, entry.to))
            }
        }

    private fun valueAt(row: JsonObject, key: String): JsonElement {
        val field = key.removePrefix(FIELDS_PREFIX)
        val value = if (field == key) row[key] else (row[FIELDS] as? JsonObject)?.get(field)
        return value ?: JsonNull
    }

    private fun textOf(value: JsonElement): String? = (value as? JsonPrimitive)?.takeIf { it.isString }?.content

    private fun JsonObject.with(key: String, value: JsonElement): JsonObject =
        JsonObject(if (value == JsonNull) this - key else this + (key to value))

    private fun JsonElement.canonical(): JsonElement = nfcStrings().withExactNumbers()

    private companion object {
        const val ID = "id"
        const val FIELDS = "fields"
        const val FIELDS_PREFIX = "fields."
        const val DELETED_AT = "deleted_at"
        val SET_KEY = Regex("[a-z][a-z_]*|fields\\.[a-z][a-z_]*")
        val UNANSWERED = setOf(TransferState.HELD.column, TransferState.PENDING.column)
    }
}
