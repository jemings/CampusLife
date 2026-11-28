package campuslife.wire

import kotlinx.serialization.json.Json

/** The contract version this client speaks, sent as CampusLife-Contract on every request (E18). */
const val CONTRACT_VERSION: Long = 1

/** Days a client keeps a Mutation after the Hub answered it, to send it again to a restored Hub (D24). */
const val SENT_RETENTION_DAYS: Long = 60

/**
 * The Json every wire model goes through. Members a newer Hub adds are skipped (E21), and
 * encodeDefaults stays false, so an optional member left at its null default is not written.
 */
val wireJson: Json = Json { ignoreUnknownKeys = true }
