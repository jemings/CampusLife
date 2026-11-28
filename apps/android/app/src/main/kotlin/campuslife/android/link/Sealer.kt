package campuslife.android.link

import javax.crypto.SecretKey

/** Seals bytes so that only the key they were sealed with opens them. */
interface Sealer {
    fun seal(plain: ByteArray): ByteArray

    /** Throws [java.security.GeneralSecurityException] when [sealed] was not sealed with this key or was changed. */
    fun open(sealed: ByteArray): ByteArray
}

/** AES-GCM under the key [key] gives, which is asked for only when bytes are sealed or opened. */
class GcmSealer(private val key: () -> SecretKey) : Sealer {
    override fun seal(plain: ByteArray): ByteArray = TODO()

    override fun open(sealed: ByteArray): ByteArray = TODO()
}
