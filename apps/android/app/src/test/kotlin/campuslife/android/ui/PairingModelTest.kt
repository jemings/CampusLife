package campuslife.android.ui

import androidx.test.ext.junit.runners.AndroidJUnit4
import campuslife.android.link.LinkRig
import campuslife.android.link.LinkRig.Companion.FIRST
import campuslife.android.link.LinkRig.Companion.FIRST_RECORD
import campuslife.android.link.LinkRig.Companion.SECOND
import campuslife.android.link.LinkRig.Companion.SECOND_HUB_ID
import campuslife.android.link.Linked
import campuslife.android.sync.FakeHub
import campuslife.android.sync.SyncOutcome
import campuslife.android.ui.PairingModel.Notice
import campuslife.android.ui.PairingModel.State
import campuslife.wire.ItemData
import campuslife.wire.ItemKind
import java.io.IOException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.serialization.json.JsonObject
import okhttp3.HttpUrl
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class PairingModelTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var rig: LinkRig
    private lateinit var model: PairingModel

    @Before
    fun open() {
        rig = LinkRig(folder.root)
        model = PairingModel(rig.link, CoroutineScope(Dispatchers.Unconfined), "phone")
    }

    @After
    fun close() = rig.close()

    @Test
    fun `the kept link is read`() {
        rig.vault.write(FIRST_RECORD)

        model.load()

        assertEquals(State(linked = FIRST_LINKED), model.state.value)
    }

    @Test
    fun `an offer read again while pairing or after it is not sent again`() {
        val offer = offer(FIRST, FakeHub.HUB_ID)
        rig.first.once({ it.path == "/v1/pair" }) { request ->
            model.scanned(offer)
            rig.first.response(request, 200, """{"device_id":"${FakeHub.DEVICE_ID}","token":"${FakeHub.TOKEN}"}""")
        }

        model.scanned(offer)
        model.scanned(offer)

        assertEquals(1, rig.first.asked("POST", "/v1/pair").size)
        assertEquals(State(linked = FIRST_LINKED, notice = Notice.PAIRED), model.state.value)
    }

    @Test
    fun `a QR code of something else says so and sends nothing`() {
        model.scanned("https://example.invalid/menu")

        assertEquals(State(notice = Notice.NOT_AN_OFFER), model.state.value)
        assertEquals(emptyList<FakeHub.Asked>(), rig.first.requests)
    }

    @Test
    fun `a typed code goes once to the typed address`() {
        model.typed("first.example.invalid", "0000-0000-0000-0000-0000-0000-0000-0000")
        assertEquals(State(linked = FIRST_LINKED, notice = Notice.PAIRED), model.state.value)

        model.typed("first.example.invalid", FakeHub.SECRET)

        assertEquals(State(linked = FIRST_LINKED, notice = Notice.SECRET_SENT), model.state.value)
        assertEquals(1, rig.first.asked("POST", "/v1/pair").size)
    }

    @Test
    fun `a typed code or address that cannot be one says so and sends nothing`() {
        model.typed("first.example.invalid", "0000")
        assertEquals(State(notice = Notice.BAD_CODE), model.state.value)

        model.typed("http://first.example.invalid", FakeHub.SECRET)
        assertEquals(State(notice = Notice.BAD_ADDRESS), model.state.value)

        assertEquals(emptyList<FakeHub.Asked>(), rig.first.requests)
    }

    @Test
    fun `a pairing that was refused, went unanswered or met another Hub says so`() {
        rig.first.once({ it.path == "/v1/pair" }) { request -> rig.first.refusal(request, 401, "unauthorized") }
        model.scanned(offer(FIRST, FakeHub.HUB_ID, "1".repeat(32)))
        assertEquals(State(notice = Notice.REFUSED), model.state.value)

        rig.first.once({ it.path == "/v1/pair" }) { throw IOException("connection cut") }
        model.scanned(offer(FIRST, FakeHub.HUB_ID, "2".repeat(32)))
        assertEquals(State(notice = Notice.NOT_REACHED), model.state.value)

        model.scanned(offer(FIRST, SECOND_HUB_ID, "3".repeat(32)))
        assertEquals(State(notice = Notice.OTHER_HUB_ANSWERED), model.state.value)
    }

    @Test
    fun `a pairing with another Hub waits for the user, who may keep the Hub there was`() {
        syncedWithFirst()

        model.scanned(offer(SECOND, SECOND_HUB_ID))
        assertEquals(State(linked = FIRST_LINKED, otherHub = 1), model.state.value)
        model.cancelOtherHub()

        assertEquals(State(linked = FIRST_LINKED), model.state.value)
        assertEquals(FIRST_RECORD, rig.vault.read())
    }

    @Test
    fun `a pairing with another Hub the user confirms moves to it`() {
        syncedWithFirst()

        model.scanned(offer(SECOND, SECOND_HUB_ID))
        model.confirmOtherHub()

        assertEquals(State(linked = Linked(SECOND, SECOND_HUB_ID, FakeHub.DEVICE_ID), notice = Notice.PAIRED), model.state.value)
        assertEquals(SECOND_HUB_ID, rig.vault.read()?.hubId)
    }

    @Test
    fun `a reissue says whether a new token came`() {
        rig.vault.write(FIRST_RECORD)
        model.load()

        model.reissue()
        assertEquals(State(linked = FIRST_LINKED, notice = Notice.REISSUED), model.state.value)

        rig.first.once({ it.path == "/v1/devices/me/token" }) { throw IOException("connection cut") }
        model.reissue()
        assertEquals(State(linked = FIRST_LINKED, notice = Notice.REISSUE_NOT_REACHED), model.state.value)

        rig.first.refuseToken = "device_revoked"
        model.reissue()
        assertEquals(State(linked = FIRST_LINKED, notice = Notice.REISSUE_REFUSED), model.state.value)
    }

    private fun syncedWithFirst() {
        rig.link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone")
        assertEquals(SyncOutcome.Synced(), rig.link.sync(false))
        rig.items.create(ItemData(kind = ItemKind.COURSE, title = "운영체제", fields = JsonObject(emptyMap())))
        model.load()
    }

    private fun offer(url: HttpUrl, hubId: String, secret: String = FakeHub.SECRET) =
        """{"hub_url":"${url.toString().removeSuffix("/")}","hub_id":"$hubId","secret":"$secret"}"""

    private companion object {
        val FIRST_LINKED = Linked(FIRST, FakeHub.HUB_ID, FakeHub.DEVICE_ID)
    }
}
