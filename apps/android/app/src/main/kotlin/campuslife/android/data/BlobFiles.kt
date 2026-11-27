package campuslife.android.data

import java.io.File
import java.io.InputStream

/** The Blob files this device keeps, one file per sha256 (decision 8). */
class BlobFiles(private val dir: File) {
    data class Kept(val sha256: String, val size: Long)

    fun file(sha256: String): File = File(dir, sha256)

    fun has(sha256: String): Boolean = file(sha256).isFile

    /** Copies [source] in, hashing it on the way. */
    fun put(source: InputStream): Kept = TODO()
}
