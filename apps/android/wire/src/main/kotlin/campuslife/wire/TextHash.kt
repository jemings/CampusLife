package campuslife.wire

import java.math.BigInteger
import java.security.MessageDigest
import java.text.Normalizer

/** from_hash of a text column (E11): sha256, lowercase hex, of the UTF-8 bytes after NFC and CRLF or CR to LF. */
fun textHash(text: String): String {
    val canonical = Normalizer.normalize(text, Normalizer.Form.NFC).replace("\r\n", "\n").replace('\r', '\n')
    val digest = MessageDigest.getInstance("SHA-256").digest(canonical.toByteArray(Charsets.UTF_8))
    return BigInteger(1, digest).toString(16).padStart(64, '0')
}
