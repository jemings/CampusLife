package campuslife.android.ui

import android.content.Context
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import campuslife.android.R
import campuslife.android.link.LinkRig
import campuslife.android.link.LinkRig.Companion.FIRST
import campuslife.android.link.LinkRig.Companion.FIRST_RECORD
import campuslife.android.sync.FakeHub
import campuslife.android.sync.SyncOutcome
import campuslife.wire.ItemData
import campuslife.wire.ItemKind
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.serialization.json.JsonObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class PairingScreenTest {
    @get:Rule
    val compose = createComposeRule()

    @get:Rule
    val folder = TemporaryFolder()

    private val context = ApplicationProvider.getApplicationContext<Context>()
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
    fun `a typed code of another Hub asks before moving, and cancelling keeps the Hub there was`() {
        rig.link.pair(FIRST, FakeHub.SECRET, FakeHub.HUB_ID, "phone")
        assertEquals(SyncOutcome.Synced(), rig.link.sync(false))
        rig.items.create(ItemData(kind = ItemKind.COURSE, title = "운영체제", fields = JsonObject(emptyMap())))
        model.load()
        compose.setContent { PairingScreen(model) {} }
        val question = context.getString(R.string.pairing_other_hub, 1)

        compose.onNodeWithText(context.getString(R.string.pairing_type)).performClick()
        compose.onNodeWithText(context.getString(R.string.pairing_hub_url)).performTextInput("second.example.invalid")
        compose.onNodeWithText(context.getString(R.string.pairing_code)).performTextInput(FakeHub.SECRET)
        compose.onNodeWithText(context.getString(R.string.pairing_connect)).performClick()
        compose.onNodeWithText(question).assertIsDisplayed()
        compose.onNodeWithText(context.getString(R.string.pairing_other_hub_cancel)).performClick()

        compose.onNodeWithText(question).assertDoesNotExist()
        assertEquals(FIRST_RECORD, rig.vault.read())
        assertEquals(FakeHub.HUB_ID, rig.store.standing()?.hubId)
    }

    @Test
    fun `a paired device shows its Hub and reissues its token`() {
        rig.vault.write(FIRST_RECORD)
        model.load()
        compose.setContent { PairingScreen(model) {} }

        compose.onNodeWithText(context.getString(R.string.pairing_hub, "https://first.example.invalid")).assertIsDisplayed()
        compose.onNodeWithText(context.getString(R.string.pairing_reissue)).performClick()

        compose.onNodeWithText(context.getString(R.string.notice_reissued)).assertIsDisplayed()
        assertEquals("fake-token-1", rig.vault.read()?.pendingToken)
    }
}
