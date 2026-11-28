package campuslife.android.sync

import java.util.concurrent.TimeUnit
import okhttp3.ConnectionPool
import okhttp3.OkHttpClient

/** The OkHttp clients this app reaches its Hub with (decision 15). Neither sends Expect: 100-continue (D22). */
object HubHttp {
    /** Idle connections are kept 60 seconds, closed before the Hub's 120-second keep-alive closes them (D22). */
    fun client(): OkHttpClient = OkHttpClient.Builder()
        .connectionPool(ConnectionPool(MAX_IDLE_CONNECTIONS, KEEP_ALIVE_SECONDS, TimeUnit.SECONDS))
        .followRedirects(false)
        .build()

    /**
     * For POST /v1/pair: OkHttp neither sends it again after a failed connection nor reuses one, since the Hub takes
     * a second pair with the same secret as a reused secret and revokes the Device it paired (D23).
     */
    fun pairingClient(): OkHttpClient = OkHttpClient.Builder()
        .retryOnConnectionFailure(false)
        .connectionPool(ConnectionPool(0, 1, TimeUnit.SECONDS))
        .followRedirects(false)
        .build()

    private const val MAX_IDLE_CONNECTIONS = 5
    private const val KEEP_ALIVE_SECONDS = 60L
}
