package campuslife.wire

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** common/cloud_policy, declared most restrictive first: the order is the policy's meaning (D8). */
@Serializable
enum class CloudPolicy {
    @SerialName("blocked") BLOCKED,
    @SerialName("local_only") LOCAL_ONLY,
    @SerialName("cloud_allowed") CLOUD_ALLOWED,
    ;

    companion object {
        fun resolve(explicit: CloudPolicy?, courseDefault: CloudPolicy?, globalDefault: CloudPolicy): CloudPolicy = TODO()

        fun effective(policies: Iterable<CloudPolicy>): CloudPolicy = TODO()
    }
}
