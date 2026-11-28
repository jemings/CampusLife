package campuslife.android.link

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.GeneralSecurityException
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Seals bytes so that only the key they were sealed with opens them. */
interface Sealer {
    fun seal(plain: ByteArray): ByteArray

    /** Throws [GeneralSecurityException] when [sealed] was not sealed with this key or was changed. */
    fun open(sealed: ByteArray): ByteArray
}

/** AES-GCM under the key [key] gives, which is asked for only when bytes are sealed or opened. */
class GcmSealer(private val key: () -> SecretKey) : Sealer {
    /** The length of the IV the cipher chose, the IV, then the ciphertext and its tag. */
    override fun seal(plain: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val iv = cipher.iv
        return byteArrayOf(iv.size.toByte()) + iv + cipher.doFinal(plain)
    }

    override fun open(sealed: ByteArray): ByteArray {
        val ivBytes = sealed.firstOrNull()?.toInt() ?: 0
        if (ivBytes <= 0 || sealed.size <= 1 + ivBytes) throw GeneralSecurityException("not sealed bytes")
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(TAG_BITS, sealed, 1, ivBytes))
        return cipher.doFinal(sealed, 1 + ivBytes, sealed.size - 1 - ivBytes)
    }

    private companion object {
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val TAG_BITS = 128
    }
}

/**
 * An AES-256 key made in the Android Keystore on first use (decision 12). It never leaves the Keystore, and is not
 * bound to the user's authentication, so a locked phone still uploads in the background.
 */
object KeystoreKey {
    fun get(alias: String): SecretKey {
        val store = KeyStore.getInstance(PROVIDER).apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        val spec = KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build()
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, PROVIDER).apply { init(spec) }.generateKey()
    }

    private const val PROVIDER = "AndroidKeyStore"
}
