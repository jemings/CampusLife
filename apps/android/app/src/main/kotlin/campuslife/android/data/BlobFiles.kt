package campuslife.android.data

import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.nio.channels.FileChannel
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption
import java.security.MessageDigest

/** The Blob files this device keeps, one file per sha256 (decision 8). */
class BlobFiles(private val dir: File) {
    data class Kept(val sha256: String, val size: Long)

    fun file(sha256: String): File = File(dir, sha256)

    fun has(sha256: String): Boolean = file(sha256).isFile

    /**
     * Copies [source] in, hashing it on the way, and puts it in place of any copy under the same sha256;
     * the file is on disk under its sha256 before this returns.
     */
    fun put(source: InputStream): Kept {
        dir.mkdirs()
        val part = File.createTempFile("incoming-", ".part", dir)
        try {
            val digest = MessageDigest.getInstance("SHA-256")
            val size = FileOutputStream(part).use { out -> pump(source, digest, out).also { out.fd.sync() } }
            val sha256 = hex(digest)
            Files.move(part.toPath(), file(sha256).toPath(), StandardCopyOption.ATOMIC_MOVE)
            syncDirectory()
            return Kept(sha256, size)
        } finally {
            part.delete()
        }
    }

    /** Whether the copy of [sha256] is here and still holds those bytes; a read that fails throws instead. */
    fun verify(sha256: String): Boolean {
        val copy = file(sha256)
        if (!copy.isFile) return false
        val digest = MessageDigest.getInstance("SHA-256")
        copy.inputStream().use { pump(it, digest, null) }
        return hex(digest) == sha256
    }

    /** Deletes the copy of [sha256], if there is one; a delete that fails throws. */
    fun drop(sha256: String) {
        Files.deleteIfExists(file(sha256).toPath())
    }

    private fun pump(source: InputStream, digest: MessageDigest, out: OutputStream?): Long {
        val buffer = ByteArray(BUFFER_BYTES)
        var size = 0L
        while (true) {
            val read = source.read(buffer)
            if (read < 0) return size
            digest.update(buffer, 0, read)
            out?.write(buffer, 0, read)
            size += read
        }
    }

    private fun hex(digest: MessageDigest): String =
        digest.digest().joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }

    private fun syncDirectory() = FileChannel.open(dir.toPath(), StandardOpenOption.READ).use { it.force(true) }

    private companion object {
        const val BUFFER_BYTES = 64 * 1024
    }
}
