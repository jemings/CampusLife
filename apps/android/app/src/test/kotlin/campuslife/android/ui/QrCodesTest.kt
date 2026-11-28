package campuslife.android.ui

import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class QrCodesTest {
    @Test
    fun `a QR code in a frame whose rows are padded reads as its text`() {
        val text = """{"hub_url":"https://hub.example.invalid","hub_id":"6f1c2b3a-4d5e-4f60-8a7b-9c0d1e2f3a4b","secret":"${"a".repeat(32)}"}"""
        val size = 240
        val stride = 256
        val code = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, size, size)
        val plane = ByteArray(stride * size) { GREY }
        for (y in 0 until size) for (x in 0 until size) plane[y * stride + x] = if (code[x, y]) BLACK else WHITE

        assertEquals(text, QrCodes.decode(plane, stride, size, size))
    }

    @Test
    fun `a frame without a code reads as nothing`() {
        assertNull(QrCodes.decode(ByteArray(64 * 64) { WHITE }, 64, 64, 64))
    }

    private companion object {
        const val BLACK: Byte = 0
        const val WHITE: Byte = -1
        const val GREY: Byte = 0x7f
    }
}
