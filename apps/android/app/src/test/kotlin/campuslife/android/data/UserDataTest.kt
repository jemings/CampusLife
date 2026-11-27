package campuslife.android.data

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import campuslife.wire.Change
import campuslife.wire.ChangesPage
import campuslife.wire.CourseFields
import campuslife.wire.CreateItem
import campuslife.wire.Delete
import campuslife.wire.EntityName
import campuslife.wire.Item
import campuslife.wire.ItemData
import campuslife.wire.ItemFields
import campuslife.wire.ItemKind
import campuslife.wire.MutableEntity
import campuslife.wire.Mutation
import campuslife.wire.MutationOutcome
import campuslife.wire.MutationResult
import campuslife.wire.MutationSerializer
import campuslife.wire.Patch
import campuslife.wire.RejectReason
import campuslife.wire.Seen
import campuslife.wire.SetEntry
import campuslife.wire.wireJson
import java.io.File
import java.text.Normalizer
import java.time.Clock
import java.time.Instant
import java.time.ZoneOffset
import java.util.UUID
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class UserDataTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val clock = Clock.fixed(Instant.parse("2026-03-02T01:00:00Z"), ZoneOffset.UTC)
    private lateinit var db: CampusLifeDatabase
    private lateinit var items: UserData
    private lateinit var sync: SyncStore
    private var rev = 0L

    @Before
    fun open() {
        reopen()
        db.syncMeta().put(SyncMeta(hubId = HUB, epoch = EPOCH, cursor = 0))
    }

    @After
    fun close() = db.close()

    /** Closes the store and opens it again on the same file, as a new process would after the old one died. */
    private fun reopen() {
        if (::db.isInitialized) db.close()
        db = Room.databaseBuilder(context, CampusLifeDatabase::class.java, File(folder.root, "store.db").path)
            .allowMainThreadQueries()
            .build()
        items = UserData(db)
        sync = SyncStore(db, BlobFiles(File(folder.root, "blobs")), clock)
    }

    @Test
    fun `a created item shows at once, pending, and goes out as a create`() {
        val fields = CourseFields(room = "공학관 301").toJson()
        val id = items.create(ItemData(kind = ItemKind.COURSE, title = "자료구조", fields = fields))

        val shown = shown().single()
        assertEquals(id to "자료구조", shown.id to shown.title)
        assertEquals(true, shown.pending)
        assertEquals(CourseFields(room = "공학관 301"), shown.typedFields())
        val create = sent().single() as CreateItem
        assertEquals(id, create.id)
        assertEquals(ItemData(kind = ItemKind.COURSE, title = "자료구조", fields = fields), create.data)
    }

    @Test
    fun `an applied create shows until the pull that brings the Hub's row`() {
        val id = items.create(ItemData(kind = ItemKind.COURSE, title = "자료구조", fields = JsonObject(emptyMap())))
        answer(MutationOutcome.APPLIED)

        assertEquals(listOf("자료구조" to false), shown().map { it.title to it.pending })

        pull(item(id, title = "자료구조 1분반"))

        assertEquals(listOf("자료구조 1분반" to false), shown().map { it.title to it.pending })
    }

    @Test
    fun `an applied patch shows until the next pull finishes, then the Hub's row shows`() {
        val id = cached(title = "A")
        items.patch(ref(id), mapOf("title" to JsonPrimitive("B")))
        answer(MutationOutcome.APPLIED)

        sync.beginPull()
        sync.applyPage(page(item(id, title = "C"), hasMore = true))
        assertEquals("B", titleOf(id))

        sync.applyPage(page(hasMore = false))
        assertEquals("C", titleOf(id))
    }

    @Test
    fun `a patch applied after a pull began stays shown through that pull and leaves with the next`() {
        val id = cached(title = "A")
        sync.beginPull()
        items.patch(ref(id), mapOf("title" to JsonPrimitive("B")))
        answer(MutationOutcome.APPLIED)

        sync.applyPage(page(hasMore = false))
        assertEquals("B", titleOf(id))

        pull()
        assertEquals("A", titleOf(id))
    }

    @Test
    fun `a pull cut short by a crash finishes with the next one`() {
        val id = cached(title = "A")
        items.patch(ref(id), mapOf("title" to JsonPrimitive("B")))
        answer(MutationOutcome.APPLIED)
        sync.beginPull()
        sync.applyPage(page(item(id, title = "C"), hasMore = true))

        reopen()

        assertEquals("B", titleOf(id))
        pull()
        assertEquals("C", titleOf(id))
    }

    @Test
    fun `a conflict takes the patch off at once and the Hub's row shows`() {
        val id = cached(title = "A")
        items.patch(ref(id), mapOf("title" to JsonPrimitive("B")))
        val mutationId = sync.nextBatch().single().mutationId

        sync.onResults(
            listOf(
                MutationResult(
                    mutationId,
                    MutationOutcome.CONFLICT,
                    appliedFields = emptyList(),
                    conflictedFields = listOf("title"),
                    conflictRecordIds = listOf("9d8e7f6a-5b4c-4d3e-8f2a-1b0c9d8e7f6a"),
                ),
            ),
        )

        assertEquals(listOf("A" to false), shown().map { it.title to it.pending })
        assertEquals(emptyList<Mutation>(), sent())
    }

    @Test
    fun `a conflict keeps the keys the Hub wrote until the next pull and shows the Hub's row for the rest`() {
        val id = cached(title = "A", fields = CourseFields(room = "1"))
        items.patch(ref(id), mapOf("title" to JsonPrimitive("B"), "fields.room" to JsonPrimitive("2")))
        val mutationId = sync.nextBatch().single().mutationId

        sync.onResults(
            listOf(
                MutationResult(
                    mutationId,
                    MutationOutcome.CONFLICT,
                    appliedFields = listOf("title"),
                    conflictedFields = listOf("fields.room"),
                ),
            ),
        )

        val shown = shown().single()
        assertEquals(Triple("B", CourseFields(room = "1"), false), Triple(shown.title, shown.typedFields(), shown.pending))

        items.patch(ref(id), mapOf("title" to JsonPrimitive("C")))
        assertEquals(mapOf("title" to SetEntry(Seen.Value(JsonPrimitive("B")), JsonPrimitive("C"))), (sent().single() as Patch).set)
        answer(MutationOutcome.APPLIED)

        pull(item(id, title = "D", fields = CourseFields(room = "3")))

        val pulled = shown().single()
        assertEquals("D" to CourseFields(room = "3"), pulled.title to pulled.typedFields())
    }

    @Test
    fun `a rejected patch comes off what is shown`() {
        val id = cached(title = "A")
        items.patch(ref(id), mapOf("title" to JsonPrimitive("B")))

        answer(MutationOutcome.REJECTED, RejectReason.OWNER)

        assertEquals("A", titleOf(id))
        assertEquals(emptyList<Mutation>(), sent())
    }

    @Test
    fun `a patch sends what was shown as from`() {
        val id = cached(title = "A", fields = CourseFields(room = "301"))

        items.patch(
            ref(id),
            mapOf("title" to JsonPrimitive("B"), "fields.room" to JsonNull, "fields.instructor" to JsonPrimitive("김교수")),
        )

        val patch = sent().single() as Patch
        assertEquals(ref(id), EntityRef(patch.entity, patch.id))
        assertEquals(
            mapOf(
                "title" to SetEntry(Seen.Value(JsonPrimitive("A")), JsonPrimitive("B")),
                "fields.room" to SetEntry(Seen.Value(JsonPrimitive("301")), JsonNull),
                "fields.instructor" to SetEntry(Seen.Value(JsonNull), JsonPrimitive("김교수")),
            ),
            patch.set,
        )
        assertEquals(CourseFields(instructor = "김교수"), shown().single().typedFields())
    }

    @Test
    fun `a patch made before the one before it is answered compares with that one`() {
        val id = cached(title = "A")
        items.patch(ref(id), mapOf("title" to JsonPrimitive("B")))
        items.patch(ref(id), mapOf("title" to JsonPrimitive("C")))

        assertEquals(
            listOf(
                SetEntry(Seen.Value(JsonPrimitive("A")), JsonPrimitive("B")),
                SetEntry(Seen.Value(JsonPrimitive("B")), JsonPrimitive("C")),
            ),
            sent().map { (it as Patch).set.getValue("title") },
        )
        assertEquals("C", titleOf(id))
    }

    @Test
    fun `body goes out as the hash of the text shown, and null when there was none`() {
        val withBody = cached(title = "A", body = "abc")
        val withoutBody = cached(title = "Z")

        items.patch(ref(withBody), mapOf("body" to JsonPrimitive("abcd")))
        items.patch(ref(withoutBody), mapOf("body" to JsonPrimitive("new")))

        assertEquals(
            listOf(Seen.TextHash("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"), Seen.TextHash(null)),
            sent().map { (it as Patch).set.getValue("body").seen },
        )
    }

    @Test
    fun `a value already shown is not sent again`() {
        val id = cached(title = "A")

        items.patch(ref(id), mapOf("title" to JsonPrimitive("A")))

        assertEquals(emptyList<Mutation>(), sent())
        assertEquals(false, shown().single().pending)
    }

    @Test
    fun `a number keeps its digits from the cache into from`() {
        val id = UUID.randomUUID().toString()
        val data = """{"id":"$id","kind":"course","schema_version":1,"title":"A","fields":{"credits":$CREDITS},"rev":1}"""
        pull(wireJson.decodeFromString(Change.serializer(), """{"entity":"item","id":"$id","rev":1,"deleted":false,"data":$data}"""))

        items.patch(ref(id), mapOf("fields.credits" to JsonPrimitive(3)))

        val body = sync.nextBatch().single().body
        assertTrue(body, body.contains(""""from":$CREDITS"""))
    }

    @Test
    fun `text goes out NFC`() {
        val decomposed = Normalizer.normalize("자료구조", Normalizer.Form.NFD)
        val id = cached(title = "A")

        items.create(ItemData(kind = ItemKind.NOTE, title = decomposed, fields = JsonObject(emptyMap())))
        items.patch(ref(id), mapOf("title" to JsonPrimitive(decomposed)))

        val (create, patch) = sent()
        assertEquals("자료구조", (create as CreateItem).data.title)
        assertEquals(JsonPrimitive("자료구조"), (patch as Patch).set.getValue("title").to)
    }

    @Test
    fun `a deleted item leaves at once and an item the Hub soft-deleted is not shown`() {
        val id = cached(title = "A")
        cached(title = "B", deletedAt = Instant.parse("2026-03-01T09:00:00Z"))

        items.delete(ref(id))

        assertEquals(emptyList<UserData.ItemView>(), shown())
        assertEquals(listOf(Delete(sync.nextBatch().single().mutationId, MutableEntity.ITEM, id)), sent())
    }

    @Test
    fun `what is not shown cannot be patched or deleted`() {
        val missing = ref(UUID.randomUUID().toString())

        assertThrows(IllegalArgumentException::class.java) { items.patch(missing, mapOf("title" to JsonPrimitive("B"))) }
        assertThrows(IllegalArgumentException::class.java) { items.delete(missing) }
        assertEquals(emptyList<Mutation>(), sent())
    }

    @Test
    fun `the feed keeps the rows of entities this version does not read, removes tombstones and moves the cursor`() {
        val id = cached(title = "A")
        val record = JsonObject(mapOf("id" to JsonPrimitive(RECORD), "note" to JsonPrimitive("2.50")))

        sync.beginPull()
        sync.applyPage(
            ChangesPage(
                hubId = HUB,
                epoch = EPOCH,
                changes = listOf(
                    Change(EntityName("conflict_record"), RECORD, 41, false, record),
                    Change(EntityName.ITEM, id, 42, true),
                ),
                nextSince = 42,
                hasMore = false,
            ),
        )

        assertEquals(emptyList<UserData.ItemView>(), shown())
        assertEquals(42L, db.syncMeta().get()?.cursor)
        assertEquals(CachedRow("conflict_record", RECORD, 41, record.toString()), db.cache().get("conflict_record", RECORD))
    }

    @Test
    fun `a number JSON cannot write is refused and nothing goes out`() {
        val id = cached(title = "A")
        for (number in listOf(Double.NaN, Double.POSITIVE_INFINITY)) {
            assertThrows(IllegalArgumentException::class.java) { items.patch(ref(id), mapOf("fields.credits" to JsonPrimitive(number))) }
            val fields = JsonObject(mapOf("credits" to JsonPrimitive(number)))
            assertThrows(IllegalArgumentException::class.java) { items.create(ItemData(kind = ItemKind.COURSE, title = "B", fields = fields)) }
        }
        assertEquals(emptyList<Mutation>(), sent())
    }

    @Test
    fun `a page before the Hub is known is refused`() {
        val fresh = Room.inMemoryDatabaseBuilder(context, CampusLifeDatabase::class.java).allowMainThreadQueries().build()
        try {
            val store = SyncStore(fresh, BlobFiles(File(folder.root, "other")), clock)
            assertThrows(IllegalStateException::class.java) { store.applyPage(page(hasMore = false)) }
        } finally {
            fresh.close()
        }
    }

    private fun ref(id: String) = EntityRef(MutableEntity.ITEM, id)

    private fun shown(): List<UserData.ItemView> = runBlocking { items.observeItems().first() }

    private fun titleOf(id: String): String = shown().single { it.id == id }.title

    private fun sent(): List<Mutation> = sync.nextBatch().map { wireJson.decodeFromString(MutationSerializer, it.body) }

    private fun answer(outcome: MutationOutcome, reason: RejectReason? = null) {
        val mutationId = sync.nextBatch().last().mutationId
        sync.onResults(listOf(MutationResult(mutationId, outcome, reason = reason)))
    }

    private fun cached(title: String, body: String? = null, fields: ItemFields? = null, deletedAt: Instant? = null): String {
        val id = UUID.randomUUID().toString()
        pull(item(id, title, body, fields, deletedAt))
        return id
    }

    private fun item(
        id: String,
        title: String,
        body: String? = null,
        fields: ItemFields? = null,
        deletedAt: Instant? = null,
    ): Change {
        rev += 1
        val row = Item(
            id = id,
            kind = ItemKind.COURSE,
            schemaVersion = 1,
            title = title,
            body = body,
            fields = fields?.toJson() ?: JsonObject(emptyMap()),
            rev = rev,
            deletedAt = deletedAt,
        )
        return Change(EntityName.ITEM, id, rev, false, wireJson.encodeToJsonElement(Item.serializer(), row).jsonObject)
    }

    private fun page(vararg changes: Change, hasMore: Boolean) =
        ChangesPage(hubId = HUB, epoch = EPOCH, changes = changes.toList(), nextSince = rev, hasMore = hasMore)

    private fun pull(vararg changes: Change) {
        sync.beginPull()
        sync.applyPage(page(*changes, hasMore = false))
    }

    private companion object {
        const val HUB = "c56a4180-65aa-42ec-a945-5fd21dec0538"
        const val EPOCH = "e4eaaaf2-d142-41ff-9e1f-2b3c4d5e6f70"
        const val CREDITS = "2.50000000000000000001"
        const val RECORD = "5f0c2b1a-8d3e-4f6a-9b7c-2e1d0a9f8b7c"
    }
}
