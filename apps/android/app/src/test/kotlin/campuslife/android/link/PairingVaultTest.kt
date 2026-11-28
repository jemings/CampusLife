package campuslife.android.link

import java.io.File
import java.security.GeneralSecurityException
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** The vault with a key held in memory, as the JVM has no Android Keystore. */
class PairingVaultTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val key = newKey()
    private val file by lazy { File(folder.root, "link") }
    private val vault by lazy { PairingVault(file, GcmSealer { key }) }

    @Test
    fun `a sealed record reads back as written`() {
        vault.write(RECORD)
        assertEquals(RECORD, PairingVault(file, GcmSealer { key }).read())

        val pending = RECORD.copy(pendingToken = "fake-token-1")
        vault.write(pending)
        assertEquals(pending, vault.read())
        assertEquals(listOf("link"), folder.root.list()!!.toList())
    }

    @Test
    fun `the file holds no token in the clear`() {
        vault.write(RECORD.copy(pendingToken = "fake-token-1"))

        val bytes = file.readBytes().toString(Charsets.ISO_8859_1)
        assertFalse(bytes.contains("fake-token"))
        assertFalse(bytes.contains(RECORD.deviceId))
    }

    @Test
    fun `no file is no link, and the key is not asked for`() {
        val vault = PairingVault(file, GcmSealer { error("the key was asked for") })

        assertNull(vault.read())
    }

    @Test
    fun `a file the key no longer opens is no link`() {
        vault.write(RECORD)

        assertNull(PairingVault(file, GcmSealer { newKey() }).read())
    }

    @Test
    fun `requests carry the token the Hub gave last`() {
        assertEquals("fake-token", RECORD.latest)
        assertEquals("fake-token-1", RECORD.copy(pendingToken = "fake-token-1").latest)
    }

    @Test
    fun `a reissued token is pending until a request with it goes through`() {
        vault.write(RECORD)

        vault.reissued("fake-token", "fake-token-1")
        assertEquals(RECORD.copy(pendingToken = "fake-token-1"), vault.read())
        vault.worked("fake-token")
        assertEquals(RECORD.copy(pendingToken = "fake-token-1"), vault.read())
        vault.worked("fake-token-1")
        assertEquals(RECORD.copy(token = "fake-token-1"), vault.read())
    }

    @Test
    fun `a reissue asked for with the pending token makes it the token, and only the last one given is used`() {
        vault.write(RECORD.copy(pendingToken = "fake-token-1"))

        vault.reissued("fake-token-1", "fake-token-2")

        assertEquals(RECORD.copy(token = "fake-token-1", pendingToken = "fake-token-2"), vault.read())
    }

    @Test
    fun `a reissue asked for with the token replaces a pending one never used`() {
        vault.write(RECORD.copy(pendingToken = "fake-token-1"))

        vault.reissued("fake-token", "fake-token-2")

        assertEquals(RECORD.copy(pendingToken = "fake-token-2"), vault.read())
    }

    @Test
    fun `GCM seals the same bytes differently each time and opens only what it sealed`() {
        val sealer = GcmSealer { key }
        val plain = "같은 내용".toByteArray()

        val first = sealer.seal(plain)
        val second = sealer.seal(plain)

        assertFalse(first.contentEquals(second))
        assertArrayEquals(plain, sealer.open(first))
        first[first.size - 1] = (first[first.size - 1].toInt() xor 1).toByte()
        assertThrows(GeneralSecurityException::class.java) { sealer.open(first) }
        assertThrows(GeneralSecurityException::class.java) { GcmSealer { newKey() }.open(second) }
    }

    private fun newKey(): SecretKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()

    private companion object {
        val RECORD = PairingVault.Record(
            hubUrl = "https://hub.example.invalid/".toHttpUrl(),
            hubId = "8a1d6f0e-3c2b-4e5f-9a7b-1c2d3e4f5a6b",
            deviceId = "5b4a3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d",
            token = "fake-token",
        )
    }
}
