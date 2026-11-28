package campuslife.android.ui

import androidx.camera.compose.CameraXViewfinder
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.core.SurfaceRequest
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.compose.LocalLifecycleOwner
import java.util.concurrent.Executors

/** The back camera's view, with every frame read for a QR code (decision 15: CameraX and ZXing, no Play services). */
@Composable
fun QrScanner(onText: (String) -> Unit, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val owner = LocalLifecycleOwner.current
    val latest by rememberUpdatedState(onText)
    var request by remember { mutableStateOf<SurfaceRequest?>(null) }
    DisposableEffect(owner) {
        val reading = Executors.newSingleThreadExecutor()
        val preview = Preview.Builder().build().apply { setSurfaceProvider { request = it } }
        val analysis = ImageAnalysis.Builder()
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .build()
            .apply { setAnalyzer(reading) { image -> image.use { QrCodes.read(it) }?.let(latest) } }
        val provider = ProcessCameraProvider.getInstance(context)
        var bound = true
        provider.addListener(
            {
                try {
                    if (bound) provider.get().bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
                } catch (_: IllegalArgumentException) {
                    // No back camera: the code can still be typed.
                }
            },
            context.mainExecutor,
        )
        onDispose {
            bound = false
            if (provider.isDone) provider.get().unbind(preview, analysis)
            reading.shutdown()
        }
    }
    request?.let { CameraXViewfinder(surfaceRequest = it, modifier = modifier) }
}
