package campuslife.android.ui

import androidx.camera.core.ImageProxy
import com.google.zxing.BinaryBitmap
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.ReaderException
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeReader

/** Reads a QR code from the luminance plane of a camera frame. */
object QrCodes {
    /** The text of a QR code in [luminance], one byte a pixel, rows [rowStride] bytes apart; null when none is read. */
    fun decode(luminance: ByteArray, rowStride: Int, width: Int, height: Int): String? {
        val source = PlanarYUVLuminanceSource(luminance, rowStride, height, 0, 0, width, height, false)
        return try {
            QRCodeReader().decode(BinaryBitmap(HybridBinarizer(source))).text
        } catch (_: ReaderException) {
            null
        }
    }

    /** A YUV_420_888 frame, whose first plane is luminance with one byte a pixel. */
    fun read(image: ImageProxy): String? {
        val plane = image.planes[0]
        val buffer = plane.buffer.duplicate()
        val bytes = ByteArray(buffer.remaining()).also { buffer.get(it) }
        return decode(bytes, plane.rowStride, image.width, image.height)
    }
}
