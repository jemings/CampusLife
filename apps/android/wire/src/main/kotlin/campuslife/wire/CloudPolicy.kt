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
        /** The explicit policy, else the course's default_cloud_policy, else the global default (A6); an explicit one may be looser. */
        fun resolve(explicit: CloudPolicy?, courseDefault: CloudPolicy?, globalDefault: CloudPolicy): CloudPolicy =
            explicit ?: courseDefault ?: globalDefault

        /** The most restrictive policy over every input of a provider call (D8). */
        fun effective(policies: Iterable<CloudPolicy>): CloudPolicy =
            requireNotNull(policies.minOrNull()) { "a provider call has at least one input" }
    }
}
