package campuslife.android.sync

import java.io.IOException
import java.security.MessageDigest
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer

/**
 * A Hub in the test process: an OkHttp interceptor that answers the sync operations as openapi/sync.yaml says, from
 * state held here. A test reads what was asked in [requests] and changes the next answers with [once].
 */
class FakeHub(
    val hubId: String = HUB_ID,
    var epoch: String = EPOCH,
    private val chunkSize: Long = 4,
) : Interceptor {
    data class Asked(
        val method: String,
        val path: String,
        val query: Map<String, String>,
        val headers: Map<String, String>,
        val body: ByteArray?,
        val thread: String,
    ) {
        fun text(): String = body?.toString(Charsets.UTF_8).orEmpty()

        fun json(): JsonObject = kotlinx.serialization.json.Json.parseToJsonElement(text()).jsonObject
    }

    private class Session(val size: Long, val chunks: MutableMap<Long, ByteArray> = mutableMapOf())

    internal data class Row(val entity: String, val id: String, val rev: Long, val data: JsonObject?)

    /** What a restore puts back: the Hub as it was when [backup] was called. */
    class Backup internal constructor(
        internal val stored: Map<String, ByteArray>,
        internal val results: Map<String, JsonObject>,
        internal val rows: Map<Pair<String, String>, Row>,
        internal val seq: Long,
    )

    val requests = mutableListOf<Asked>()
    val statuses = mutableListOf<JsonObject>()
    val stored = mutableMapOf<String, ByteArray>()
    var token = TOKEN

    /** The token POST /v1/devices/me/token gave last; its first use makes it [token] and ends the one before (D23). */
    var pendingToken: String? = null
    var minContractVersion = 1L
    var storageFull = false

    /** Answers every authenticated request with 401 and this code while set. */
    var refuseToken: String? = null

    private val sessions = mutableMapOf<String, Session>()
    private val results = mutableMapOf<String, JsonObject>()
    private val rows = linkedMapOf<Pair<String, String>, Row>()
    private var seq = 0L
    private var reissues = 0
    private val overrides = mutableListOf<Pair<(Asked) -> Boolean, (Request) -> Response>>()

    /** The next request [match] takes is answered by [answer], which may throw to cut the connection. */
    @Synchronized
    fun once(match: (Asked) -> Boolean, answer: (Request) -> Response) {
        overrides += match to answer
    }

    @Synchronized
    fun backup(): Backup = Backup(stored.toMap(), results.toMap(), rows.toMap(), seq)

    /** The Hub restored from [backup] under a new [epoch]: what came after the backup is gone (D24). */
    @Synchronized
    fun restore(backup: Backup, epoch: String) {
        stored.clear()
        stored.putAll(backup.stored)
        results.clear()
        results.putAll(backup.results)
        rows.clear()
        rows.putAll(backup.rows)
        seq = backup.seq
        sessions.clear()
        this.epoch = epoch
    }

    /** Puts a row in the changes feed, as another device's write would. */
    @Synchronized
    fun write(entity: String, id: String, data: JsonObject) {
        rows[entity to id] = Row(entity, id, ++seq, data)
    }

    fun row(entity: String, id: String): JsonObject? = rows[entity to id]?.data

    fun asked(method: String, path: String): List<Asked> = requests.filter { it.method == method && it.path == path }

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val body = request.body?.let { Buffer().also(it::writeTo).readByteArray() }
        val asked = Asked(
            request.method,
            request.url.encodedPath,
            request.url.queryParameterNames.associateWith { request.url.queryParameter(it).orEmpty() },
            request.headers.names().associateWith { request.header(it).orEmpty() },
            body,
            Thread.currentThread().name,
        )
        val override = synchronized(this) {
            requests += asked
            overrides.firstOrNull { (match) -> match(asked) }?.also { overrides.remove(it) }?.second
        }
        if (override != null) return override(request)
        return synchronized(this) { answer(request, asked) }
    }

    private fun answer(request: Request, asked: Asked): Response {
        if (asked.headers["CampusLife-Contract"] != "1") return refusal(request, 400, "invalid_request")
        if (asked.path == "/v1/pair") return pair(request, asked)
        refuseToken?.let { return refusal(request, 401, it) }
        val bearer = asked.headers["Authorization"]?.takeIf { it.startsWith("Bearer ") }?.removePrefix("Bearer ")
        if (bearer != null && bearer == pendingToken) {
            token = bearer
            pendingToken = null
        }
        if (bearer != token) return refusal(request, 401, "unauthorized")
        if (asked.path == "/v1/changes" || asked.path == "/v1/mutations") {
            val base = asked.headers["CampusLife-Epoch"]
            if (base == null || !EPOCH_FORM.matches(base)) return refusal(request, 400, "invalid_request")
            if (base != epoch) return refusal(request, 409, "epoch_changed", buildJsonObject { put("epoch", epoch) })
        }
        val parts = asked.path.removePrefix("/").split("/")
        return when {
            asked.method == "GET" && asked.path == "/v1/hub" -> ok(
                request,
                200,
                buildJsonObject {
                    put("hub_id", hubId)
                    put("epoch", epoch)
                    put("contract_version", 1)
                    put("min_contract_version", minContractVersion)
                },
            )
            asked.method == "POST" && asked.path == "/v1/blobs/uploads" -> openUpload(request, asked.json())
            asked.method == "PUT" && parts.size == 6 && parts[4] == "chunks" -> chunk(request, parts[3], parts[5].toLong(), asked.body!!)
            asked.method == "POST" && parts.size == 5 && parts[4] == "finalize" -> finish(request, parts[3])
            asked.method == "POST" && asked.path == "/v1/mutations" -> mutations(request, asked.text())
            asked.method == "GET" && asked.path == "/v1/changes" -> changes(request, asked.query.getValue("since").toLong(), asked.query.getValue("limit").toInt())
            asked.method == "POST" && asked.path == "/v1/devices/me/token" -> {
                val issued = "$TOKEN-${++reissues}"
                pendingToken = issued
                ok(request, 200, buildJsonObject {
                    put("device_id", DEVICE_ID)
                    put("token", issued)
                })
            }
            asked.method == "POST" && asked.path == "/v1/devices/me/status" -> {
                statuses += asked.json()
                empty(request, 204)
            }
            else -> refusal(request, 404, "not_found")
        }
    }

    private fun pair(request: Request, asked: Asked): Response {
        if (asked.json()["secret"]?.jsonPrimitive?.content != SECRET) return refusal(request, 401, "unauthorized")
        return ok(request, 200, buildJsonObject {
            put("device_id", DEVICE_ID)
            put("token", token)
        })
    }

    private fun openUpload(request: Request, body: JsonObject): Response {
        val sha256 = body.getValue("sha256").jsonPrimitive.content
        val size = body.getValue("size").jsonPrimitive.content.toLong()
        stored[sha256]?.let { return ok(request, 200, blob(sha256, it.size.toLong())) }
        if (storageFull) return refusal(request, 400, "hub_storage_full")
        val session = sessions.getOrPut(sha256) { Session(size) }
        if (session.size != size) return refusal(request, 400, "invalid_request")
        return ok(request, 201, buildJsonObject {
            put("sha256", sha256)
            put("size", size)
            put("chunk_size", chunkSize)
            put("received", buildJsonArray { session.chunks.keys.sorted().forEach { add(JsonPrimitive(it)) } })
        })
    }

    private fun chunk(request: Request, sha256: String, index: Long, bytes: ByteArray): Response {
        val session = sessions[sha256] ?: return refusal(request, 404, "not_found")
        val count = chunkCount(session.size)
        val length = minOf(chunkSize, session.size - index * chunkSize)
        if (index >= count || bytes.size.toLong() != length) return refusal(request, 400, "invalid_request")
        val before = session.chunks[index]
        if (before != null && !before.contentEquals(bytes)) return refusal(request, 409, "chunk_mismatch")
        session.chunks[index] = bytes
        return empty(request, 204)
    }

    private fun finish(request: Request, sha256: String): Response {
        stored[sha256]?.let { return ok(request, 200, blob(sha256, it.size.toLong())) }
        val session = sessions[sha256] ?: return refusal(request, 404, "not_found")
        val missing = (0 until chunkCount(session.size)).filter { it !in session.chunks }
        if (missing.isNotEmpty()) {
            return refusal(request, 409, "incomplete", buildJsonObject {
                put("missing_chunks", buildJsonArray { missing.forEach { add(JsonPrimitive(it)) } })
            })
        }
        val bytes = (0 until chunkCount(session.size)).map { session.chunks.getValue(it) }.fold(ByteArray(0)) { all, part -> all + part }
        sessions.remove(sha256)
        if (digestOf(bytes) != sha256) return refusal(request, 409, "hash_mismatch")
        stored[sha256] = bytes
        return ok(request, 200, blob(sha256, bytes.size.toLong()))
    }

    private fun mutations(request: Request, text: String): Response {
        val batch = kotlinx.serialization.json.Json.parseToJsonElement(text).jsonObject.getValue("mutations").jsonArray
        if (batch.any { !readable(it) }) return refusal(request, 400, "invalid_request")
        var stopped = false
        val answered = batch.map { element ->
            val mutation = element.jsonObject
            val id = mutation.getValue("mutation_id").jsonPrimitive.content
            when {
                stopped -> result(id, "not_attempted")
                id in results -> buildJsonObject {
                    put("mutation_id", id)
                    put("result", "duplicate")
                    put("original", results.getValue(id))
                }
                else -> judge(id, mutation).also { if (it.getValue("result").jsonPrimitive.content == "retry") stopped = true }
            }
        }
        return ok(request, 200, buildJsonObject { put("results", JsonArray(answered)) })
    }

    /** As the Hub's loads_strict: no number past a double (D22 judging order step 4). */
    private fun readable(element: JsonElement): Boolean = when (element) {
        is JsonObject -> element.values.all(::readable)
        is JsonArray -> element.all(::readable)
        is JsonNull -> true
        is JsonPrimitive -> element.isString || element.content in setOf("true", "false") || element.content.toDouble().isFinite()
    }

    private fun judge(id: String, mutation: JsonObject): JsonObject {
        val decided = when (mutation.getValue("op").jsonPrimitive.content) {
            "capture.commit" -> commit(id, mutation.getValue("capture").jsonObject) ?: return result(id, "retry") {
                put("missing_blobs", missing(mutation.getValue("capture").jsonObject))
            }
            "create" -> {
                val entity = mutation.getValue("entity").jsonPrimitive.content
                val itemId = mutation.getValue("id").jsonPrimitive.content
                write(entity, itemId, JsonObject(mapOf("id" to JsonPrimitive(itemId)) + mutation.getValue("data").jsonObject))
                result(id, "applied")
            }
            else -> result(id, "applied")
        }
        results[id] = decided
        return decided
    }

    private fun commit(id: String, capture: JsonObject): JsonObject? {
        if (missing(capture).isNotEmpty()) return null
        val captureId = capture.getValue("id").jsonPrimitive.content
        write("capture", captureId, JsonObject(capture - "attachments"))
        for (attachment in capture.getValue("attachments").jsonArray) {
            val attachmentId = attachment.jsonObject.getValue("id").jsonPrimitive.content
            write("attachment", attachmentId, JsonObject(attachment.jsonObject + ("capture_id" to JsonPrimitive(captureId))))
        }
        return result(id, "applied")
    }

    private fun missing(capture: JsonObject): JsonArray = buildJsonArray {
        capture.getValue("attachments").jsonArray
            .map { it.jsonObject.getValue("blob_sha256").jsonPrimitive.content }
            .filter { it !in stored }
            .distinct()
            .forEach { add(JsonPrimitive(it)) }
    }

    private fun changes(request: Request, since: Long, limit: Int): Response {
        val after = rows.values.filter { it.rev > since }.sortedBy { it.rev }
        val page = after.take(limit)
        return ok(request, 200, buildJsonObject {
            put("hub_id", hubId)
            put("epoch", epoch)
            put("changes", buildJsonArray {
                for (row in page) {
                    add(buildJsonObject {
                        put("entity", row.entity)
                        put("id", row.id)
                        put("rev", row.rev)
                        put("deleted", row.data == null)
                        row.data?.let { put("data", it) }
                    })
                }
            })
            put("next_since", page.lastOrNull()?.rev ?: since)
            put("has_more", after.size > page.size)
        })
    }

    private fun chunkCount(size: Long): Long = (size + chunkSize - 1) / chunkSize

    private fun blob(sha256: String, size: Long) = buildJsonObject {
        put("sha256", sha256)
        put("size", size)
    }

    private fun result(id: String, result: String, more: kotlinx.serialization.json.JsonObjectBuilder.() -> Unit = {}) = buildJsonObject {
        put("mutation_id", id)
        put("result", result)
        more()
    }

    private fun ok(request: Request, status: Int, body: JsonObject): Response = response(request, status, body.toString())

    private fun empty(request: Request, status: Int): Response = response(request, status, "")

    fun refusal(request: Request, status: Int, code: String, details: JsonObject? = null): Response =
        response(request, status, buildJsonObject {
            put("code", code)
            put("message", "refused: $code")
            details?.let { put("details", it) }
        }.toString())

    fun response(request: Request, status: Int, text: String, hub: String? = hubId): Response = Response.Builder()
        .request(request)
        .protocol(Protocol.HTTP_1_1)
        .code(status)
        .message("fake")
        .apply { if (hub != null) header("CampusLife-Hub-Id", hub) }
        .header("CampusLife-Epoch", epoch)
        .body(text.toResponseBody(JSON))
        .build()

    companion object {
        const val HUB_ID = "8a1d6f0e-3c2b-4e5f-9a7b-1c2d3e4f5a6b"
        const val EPOCH = "0d9c8b7a-6f5e-4d3c-8b2a-1f0e9d8c7b6a"
        const val TOKEN = "fake-token"
        const val SECRET = "00000000000000000000000000000000"
        const val DEVICE_ID = "5b4a3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d"
        private val JSON = "application/json".toMediaType()

        /** The Hub's envelope refuses a CampusLife-Epoch that is not a lowercase UUIDv4 with 400 before the body. */
        private val EPOCH_FORM = Regex("[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

        fun digestOf(bytes: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }

        /** An override that drops the connection, as a phone leaving coverage would. */
        val cut: (Request) -> Response = { throw IOException("connection cut") }
    }
}
