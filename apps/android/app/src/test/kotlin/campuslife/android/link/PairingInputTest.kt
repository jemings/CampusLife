package campuslife.android.link

import campuslife.wire.PairingOffer
import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PairingInputTest {
    private val fixtures = File(System.getProperty("campuslife.contract.fixtures")!!)

    @Test
    fun `every valid and forward offer fixture is an offer`() {
        val offers = offers("valid") + offers("forward")
        assertTrue(offers.size >= 4)

        for (file in offers) {
            val members = Json.parseToJsonElement(file.readText()).jsonObject
            val expected = PairingOffer(
                members.getValue("hub_url").jsonPrimitive.content,
                members.getValue("hub_id").jsonPrimitive.content,
                members.getValue("secret").jsonPrimitive.content,
            )
            assertEquals(file.name, expected, PairingInput.offer(file.readText()))
        }
    }

    @Test
    fun `no invalid offer fixture is an offer`() {
        val offers = offers("invalid")
        assertTrue(offers.size >= 13)

        for (file in offers) assertNull(file.name, PairingInput.offer(file.readText()))
    }

    @Test
    fun `a QR code of anything else is no offer`() {
        val texts = listOf(
            "https://example.invalid/menu",
            "[]",
            "\"text\"",
            """{"hub_url":"https://hub.example.invalid","hub_id":"6f1c2b3a-4d5e-4f60-8a7b-9c0d1e2f3a4b"}""",
            """{"hub_url":"https://hub.example.invalid","hub_id":"6f1c2b3a-4d5e-4f60-8a7b-9c0d1e2f3a4b","secret":7}""",
            """{"hub_url":"https://${"a".repeat(260)}.invalid","hub_id":"6f1c2b3a-4d5e-4f60-8a7b-9c0d1e2f3a4b","secret":"$SECRET"}""",
        )

        for (text in texts) assertNull(text, PairingInput.offer(text))
    }

    @Test
    fun `a typed code drops spaces and hyphens and is lowercased`() {
        assertEquals(SECRET, PairingInput.secret("0123-4567-89AB-cdef 0123-4567-89ab-CDEF"))
        assertEquals(SECRET, PairingInput.secret("  0123456789abcdef0123456789abcdef\n"))
    }

    @Test
    fun `a typed code that is not 32 hex digits is no secret`() {
        for (code in listOf("", "0123-4567", SECRET + "0", SECRET.dropLast(1) + "g", "0123_4567_89ab_cdef_0123_4567_89ab_cdef")) {
            assertNull(code, PairingInput.secret(code))
        }
    }

    @Test
    fun `a typed address is the Hub's origin over https`() {
        assertEquals("https://laptop.tailnet.ts.net/".toHttpUrl(), PairingInput.hubUrl("laptop.tailnet.ts.net"))
        assertEquals("https://laptop.tailnet.ts.net:8443/".toHttpUrl(), PairingInput.hubUrl(" https://Laptop.Tailnet.ts.net:8443/ "))
    }

    @Test
    fun `a typed address that is not a Hub's origin is refused`() {
        val typed = listOf(
            "",
            "http://laptop.tailnet.ts.net",
            "https://laptop.tailnet.ts.net/v1",
            "https://laptop.tailnet.ts.net?x=1",
            "https://user@laptop.tailnet.ts.net",
            "https://laptop.tailnet.ts.net:0",
            "https://laptop.tailnet.ts.net:65536",
            "https://lap_top.tailnet.ts.net",
        )

        for (text in typed) assertNull(text, PairingInput.hubUrl(text))
    }

    @Test
    fun `a device name is one line of at most 64 characters`() {
        assertEquals("Galaxy Z Fold8", PairingInput.deviceName("Galaxy Z Fold8"))
        assertEquals("GalaxyZ Fold8", PairingInput.deviceName("Galaxy\n\u202eZ\u0007 Fold8"))
        assertEquals("가".repeat(64), PairingInput.deviceName("가".repeat(70)))
        assertEquals(PHONE.repeat(64), PairingInput.deviceName(PHONE.repeat(65)))
        assertEquals("Android", PairingInput.deviceName("\u0001\u2028"))
        assertEquals("Android", PairingInput.deviceName(null))
    }

    private fun offers(kind: String): List<File> =
        File(fixtures, "$kind/device/pairing_offer").listFiles { file -> file.name.endsWith(".json") && !file.name.endsWith(".expect.json") }
            .orEmpty()
            .sortedBy { it.name }

    private companion object {
        const val SECRET = "0123456789abcdef0123456789abcdef"

        /** One code point in two UTF-16 units. */
        const val PHONE = "\uD83D\uDCF1"
    }
}
