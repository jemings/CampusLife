package campuslife.android.data

import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest

/** The Blob files this device keeps, one file per sha256 (decision 8). */
class BlobFiles(private val dir: File) {
    data class Kept(val sha256: String, val size: Long)

    fun file(sha256: String): File = File(dir, sha256)

    fun has(sha256: String): Boolean = file(sha256).isFile

    /** Copies [source] in, hashing it on the way; the file is on disk under its sha256 before this returns. */
    fun put(source: InputStream): Kept {
        dir.mkdirs()
        val part = File.createTempFile("incoming-", ".part", dir)
        try {
            val digest = MessageDigest.getInstance("SHA-256")
            var size = 0L
            FileOutputStream(part).use { out ->
                val buffer = ByteArray(BUFFER_BYTES)
                while (true) {
                    val read = source.read(buffer)
                    if (read < 0) break
                    digest.update(buffer, 0, read)
                    out.write(buffer, 0, read)
                    size += read
                }
                out.fd.sync()
            }
            val sha256 = digest.digest().joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }
            val kept = file(sha256)
            if (!kept.isFile && !part.renameTo(kept)) throw IOException("could not keep $kept")
            return Kept(sha256, size)
        } finally {
            part.delete()
        }
    }

    private companion object {
        const val BUFFER_BYTES = 64 * 1024
    }
}
