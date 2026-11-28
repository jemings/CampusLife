package campuslife.android.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.annotation.StringRes
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import campuslife.android.R
import campuslife.android.ui.PairingModel.Notice

private enum class Mode { IDLE, SCANNING, TYPING }

/** Pairing with the Hub by QR code or typed code, the link kept, and a token reissue; one compact pane (decision 18). */
@Composable
fun PairingScreen(model: PairingModel, scanner: @Composable (onText: (String) -> Unit) -> Unit) {
    val state by model.state.collectAsState()
    var mode by rememberSaveable { mutableStateOf(Mode.IDLE) }
    LaunchedEffect(state.notice) { if (state.notice == Notice.PAIRED) mode = Mode.IDLE }
    Column(
        Modifier
            .fillMaxSize()
            .safeDrawingPadding()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(stringResource(R.string.app_name), style = MaterialTheme.typography.headlineSmall)
        val linked = state.linked
        if (linked == null) {
            Text(stringResource(R.string.pairing_not_linked))
        } else {
            Text(stringResource(R.string.pairing_hub, linked.hubUrl.toString().removeSuffix("/")))
            Text(stringResource(R.string.pairing_device, linked.deviceId))
            OutlinedButton(onClick = model::reissue, enabled = !state.busy) { Text(stringResource(R.string.pairing_reissue)) }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { mode = Mode.SCANNING }, enabled = !state.busy) { Text(stringResource(R.string.pairing_scan)) }
            OutlinedButton(onClick = { mode = Mode.TYPING }, enabled = !state.busy) { Text(stringResource(R.string.pairing_type)) }
        }
        when (mode) {
            Mode.IDLE -> Unit
            Mode.SCANNING -> Scanning(model, scanner)
            Mode.TYPING -> Typing(model, state.busy)
        }
        if (state.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
        state.notice?.let { Text(stringResource(it.text)) }
    }
    state.otherHub?.let { unsent ->
        AlertDialog(
            onDismissRequest = model::cancelOtherHub,
            confirmButton = { TextButton(onClick = model::confirmOtherHub) { Text(stringResource(R.string.pairing_other_hub_confirm)) } },
            dismissButton = { TextButton(onClick = model::cancelOtherHub) { Text(stringResource(R.string.pairing_other_hub_cancel)) } },
            title = { Text(stringResource(R.string.pairing_other_hub_title)) },
            text = { Text(stringResource(R.string.pairing_other_hub, unsent)) },
        )
    }
}

@Composable
private fun Scanning(model: PairingModel, scanner: @Composable (onText: (String) -> Unit) -> Unit) {
    val context = LocalContext.current
    var granted by remember {
        mutableStateOf(context.checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED)
    }
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted = it }
    LaunchedEffect(Unit) { if (!granted) ask.launch(Manifest.permission.CAMERA) }
    if (granted) {
        Box(Modifier.fillMaxWidth().aspectRatio(1f)) { scanner(model::scanned) }
    } else {
        Text(stringResource(R.string.pairing_camera_needed))
    }
}

@Composable
private fun Typing(model: PairingModel, busy: Boolean) {
    var hubUrl by rememberSaveable { mutableStateOf("") }
    var code by rememberSaveable { mutableStateOf("") }
    OutlinedTextField(
        value = hubUrl,
        onValueChange = { hubUrl = it },
        modifier = Modifier.fillMaxWidth(),
        label = { Text(stringResource(R.string.pairing_hub_url)) },
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
    )
    OutlinedTextField(
        value = code,
        onValueChange = { code = it },
        modifier = Modifier.fillMaxWidth(),
        label = { Text(stringResource(R.string.pairing_code)) },
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii),
    )
    Button(onClick = { model.typed(hubUrl, code) }, enabled = !busy) { Text(stringResource(R.string.pairing_connect)) }
}

@get:StringRes
private val Notice.text: Int
    get() = when (this) {
        Notice.NOT_AN_OFFER -> R.string.notice_not_an_offer
        Notice.BAD_CODE -> R.string.notice_bad_code
        Notice.BAD_ADDRESS -> R.string.notice_bad_address
        Notice.SECRET_SENT -> R.string.notice_secret_sent
        Notice.PAIRED -> R.string.notice_paired
        Notice.REFUSED -> R.string.notice_refused
        Notice.NOT_REACHED -> R.string.notice_not_reached
        Notice.OTHER_HUB_ANSWERED -> R.string.notice_other_hub_answered
        Notice.FAILED -> R.string.notice_failed
        Notice.REISSUED -> R.string.notice_reissued
        Notice.REISSUE_REFUSED -> R.string.notice_reissue_refused
        Notice.REISSUE_NOT_REACHED -> R.string.notice_reissue_not_reached
    }
