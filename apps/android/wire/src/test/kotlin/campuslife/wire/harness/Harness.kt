package campuslife.wire.harness

import campuslife.wire.CloudPolicy
import campuslife.wire.textHash
import campuslife.wire.wireJson
import java.nio.file.Files
import java.nio.file.Path
import kotlin.io.path.createDirectories
import kotlin.io.path.deleteIfExists
import kotlin.io.path.isDirectory
import kotlin.io.path.isRegularFile
import kotlin.io.path.name
import kotlin.io.path.readText
import kotlin.io.path.writeText
import kotlin.system.exitProcess
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** What one run found. Only forward and vector problems fail the run itself; the comparator judges the outputs. */
class HarnessReport(
    val written: Int,
    val forwardRead: Int,
    val vectorsRun: Int,
    val validProblems: List<String>,
    val forwardProblems: List<String>,
    val vectorProblems: List<String>,
) {
    val problems: List<String> get() = validProblems + forwardProblems + vectorProblems
}

/** The protocol of packages/api-contracts/HARNESS.md: valid fixtures round-trip into [out], forward ones decode, vectors agree. */
fun runHarness(fixtureRoot: Path, out: Path): HarnessReport {
    val validProblems = mutableListOf<String>()
    var written = 0
    for (fixture in fixtures(fixtureRoot.resolve("valid"))) {
        val target = out.resolve(fixture.schemaId).resolve(fixture.path.name)
        runCatching { codecFor(fixture.schemaId).roundTrip(fixture.path.readText()) }
            .onSuccess {
                target.parent.createDirectories()
                target.writeText(it)
                written++
            }
            .onFailure {
                target.deleteIfExists()
                validProblems += "valid/$fixture: $it"
            }
    }
    val forward = fixtures(fixtureRoot.resolve("forward"))
    val forwardProblems = forward.mapNotNull { fixture ->
        runCatching { codecFor(fixture.schemaId).read(fixture.path.readText()) }
            .exceptionOrNull()
            ?.let { "forward/$fixture: $it" }
    }
    val vectors = vectorChecks(fixtureRoot)
    val vectorProblems = vectors.mapNotNull { it() }
    return HarnessReport(written, forward.size, vectors.size, validProblems, forwardProblems, vectorProblems)
}

fun main(args: Array<String>) {
    if (args.size != 2) {
        System.err.println("usage: HarnessKt <fixture root> <output directory>")
        exitProcess(2)
    }
    val report = runHarness(Path.of(args[0]), Path.of(args[1]))
    report.problems.forEach(::println)
    println("${report.written} outputs written, ${report.forwardRead} forward fixtures read, ${report.vectorsRun} vectors run")
    exitProcess(if (report.forwardProblems.isEmpty() && report.vectorProblems.isEmpty()) 0 else 1)
}

private class Fixture(val schemaId: String, val path: Path) {
    override fun toString() = "$schemaId/${path.name}"
}

private fun fixtures(base: Path): List<Fixture> =
    jsonFiles(base).map { Fixture(base.relativize(it.parent).joinToString("/"), it) }

private fun jsonFiles(dir: Path): List<Path> =
    if (!dir.isDirectory()) {
        emptyList()
    } else {
        Files.walk(dir).use { paths ->
            paths.iterator().asSequence().filter { it.isRegularFile() && it.name.endsWith(".json") }.sorted().toList()
        }
    }

private fun codecFor(schemaId: String): Codec<*> = codecs[schemaId] ?: error("no Kotlin model for $schemaId")

@Serializable
private class ResolveVector(
    val explicit: CloudPolicy?,
    @SerialName("course_default") val courseDefault: CloudPolicy?,
    @SerialName("global_default") val globalDefault: CloudPolicy,
    val expected: CloudPolicy,
)

@Serializable
private class MinVector(val policies: List<CloudPolicy>, val expected: CloudPolicy)

@Serializable
private class TextHashVector(val text: String, val hash: String)

/** One check per vector file; a check gives null when the Kotlin function agrees with the vector. */
private fun vectorChecks(root: Path): List<() -> String?> =
    jsonFiles(root.resolve("policy").resolve("resolve")).map { path ->
        agrees(root, path, ResolveVector.serializer()) {
            CloudPolicy.resolve(it.explicit, it.courseDefault, it.globalDefault) to it.expected
        }
    } + jsonFiles(root.resolve("policy").resolve("min")).map { path ->
        agrees(root, path, MinVector.serializer()) { CloudPolicy.effective(it.policies) to it.expected }
    } + jsonFiles(root.resolve("text_hash")).map { path ->
        agrees(root, path, TextHashVector.serializer()) { textHash(it.text) to it.hash }
    }

private fun <V> agrees(root: Path, path: Path, serializer: KSerializer<V>, compute: (V) -> Pair<Any, Any>): () -> String? = {
    val name = root.relativize(path).joinToString("/")
    runCatching { compute(wireJson.decodeFromString(serializer, path.readText())) }.fold(
        onSuccess = { (actual, expected) -> if (actual == expected) null else "$name: gives $actual, the vector expects $expected" },
        onFailure = { "$name: $it" },
    )
}
