package campuslife.android

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import campuslife.android.ui.PairingScreen
import campuslife.android.ui.QrScanner

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val pairing = (application as CampusLifeApplication).graph.pairing
        pairing.load()
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    PairingScreen(pairing) { onText -> QrScanner(onText, Modifier.fillMaxSize()) }
                }
            }
        }
    }
}
