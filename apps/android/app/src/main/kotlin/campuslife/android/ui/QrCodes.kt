package campuslife.android.ui

/** Reads a QR code from the luminance plane of a camera frame. */
object QrCodes {
    /** The text of a QR code in [luminance], one byte a pixel, rows [rowStride] bytes apart; null when none is read. */
    fun decode(luminance: ByteArray, rowStride: Int, width: Int, height: Int): String? = TODO()
}
