package campuslife.android.sync

import androidx.test.ext.junit.runners.AndroidJUnit4
import campuslife.wire.ErrorCode
import campuslife.wire.PairRequest
import campuslife.wire.Platform
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class PairingTest {
    private val hub = FakeHub()
    private val pairing = Pairing(OkHttpClient.Builder().addInterceptor(hub).build())

    @Test
    fun `a pair sends the secret without a token and keeps the id of the Hub that answered`() {
        val paired = pairing.pair(URL, request(FakeHub.SECRET), expectedHubId = FakeHub.HUB_ID)

        assertEquals(Pairing.Paired(FakeHub.HUB_ID, FakeHub.DEVICE_ID, FakeHub.TOKEN), paired)
        val asked = hub.requests.single()
        assertEquals("POST /v1/pair", "${asked.method} ${asked.path}")
        assertEquals("1", asked.headers["CampusLife-Contract"])
        assertNull(asked.headers["Authorization"])
    }

    @Test
    fun `an answer from a Hub other than the offer's pairs nothing`() {
        val other = FakeHub(hubId = OTHER_HUB_ID)

        val trouble = assertThrows(HubTrouble.OtherHub::class.java) {
            Pairing(OkHttpClient.Builder().addInterceptor(other).build()).pair(URL, request(FakeHub.SECRET), expectedHubId = FakeHub.HUB_ID)
        }

        assertEquals(OTHER_HUB_ID, trouble.hubId)
    }

    @Test
    fun `a refused secret says when the Hub takes another try`() {
        hub.once({ it.path == "/v1/pair" }) { request ->
            hub.refusal(request, 401, "unauthorized").newBuilder().header("Retry-After", "30").build()
        }

        val refused = assertThrows(Pairing.Refused::class.java) { pairing.pair(URL, request(FakeHub.SECRET)) }

        assertEquals(401, refused.status)
        assertEquals(ErrorCode.UNAUTHORIZED, refused.error?.code)
        assertEquals(30L, refused.retryAfterSeconds)
    }

    @Test
    fun `an answer without the Hub's id is a Hub not reached`() {
        hub.once({ it.path == "/v1/pair" }) { request -> hub.response(request, 502, "Bad Gateway", hub = null) }

        assertThrows(HubTrouble.NotReached::class.java) { pairing.pair(URL, request(FakeHub.SECRET)) }
    }

    private fun request(secret: String) = PairRequest(secret, Platform.ANDROID, "phone", "0.1.0")

    private companion object {
        val URL = "http://hub.test/".toHttpUrl()
        const val OTHER_HUB_ID = "9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a"
    }
}
