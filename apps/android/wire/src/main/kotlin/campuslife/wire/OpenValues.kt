package campuslife.wire

import kotlinx.serialization.Serializable

// Open values are ^[a-z][a-z_]*$ strings the Hub may extend (HARNESS.md). A value this build does
// not know decodes and is kept as it came; the constants are the contract's known values.

/** item/item kind. A kind this build does not know keeps its title, dates and body (E21). */
@Serializable
@JvmInline
value class ItemKind(val value: String) {
    companion object {
        val TERM = ItemKind("term")
        val COURSE = ItemKind("course")
        val LECTURE_SESSION = ItemKind("lecture_session")
        val ASSIGNMENT = ItemKind("assignment")
        val EXAM = ItemKind("exam")
        val TASK = ItemKind("task")
        val NOTE = ItemKind("note")
    }
}

/** Attachment role; a client sends only the known ones. */
@Serializable
@JvmInline
value class AttachmentRole(val value: String) {
    companion object {
        val AUDIO_SEGMENT = AttachmentRole("audio_segment")
        val PHOTO = AttachmentRole("photo")
        val FILE = AttachmentRole("file")
        val WEB_SNAPSHOT = AttachmentRole("web_snapshot")
        val TEXT = AttachmentRole("text")
    }
}

/** Capture origin type; a client sends only the known ones. */
@Serializable
@JvmInline
value class OriginType(val value: String) {
    companion object {
        val RECORD = OriginType("record")
        val CAMERA = OriginType("camera")
        val SHARE = OriginType("share")
        val IMPORT = OriginType("import")
        val MANUAL = OriginType("manual")
    }
}

/** Relation type; a create sends only the known ones. */
@Serializable
@JvmInline
value class RelationType(val value: String) {
    companion object {
        val ABOUT = RelationType("about")
        val SUPERSEDES = RelationType("supersedes")
        val RENDITION_OF = RelationType("rendition_of")
        val EVIDENCE = RelationType("evidence")
        val TAGGED = RelationType("tagged")
        val CONFLICTS_WITH = RelationType("conflicts_with")
    }
}

/** The entity of a changes row; a client skips rows of entities it does not know. */
@Serializable
@JvmInline
value class EntityName(val value: String) {
    companion object {
        val CAPTURE = EntityName("capture")
        val ATTACHMENT = EntityName("attachment")
        val ITEM = EntityName("item")
        val RELATION = EntityName("relation")
        val TAG = EntityName("tag")
        val ARTIFACT = EntityName("artifact")
        val SUGGESTION = EntityName("suggestion")
        val CONFLICT_RECORD = EntityName("conflict_record")
    }
}

/** common/error code; a code this build does not know is a plain failure. */
@Serializable
@JvmInline
value class ErrorCode(val value: String) {
    companion object {
        val INVALID_REQUEST = ErrorCode("invalid_request")
        val NOT_CANONICAL = ErrorCode("not_canonical")
        val ID_REUSE = ErrorCode("id_reuse")
        val HASH_MISMATCH = ErrorCode("hash_mismatch")
        val CHUNK_MISMATCH = ErrorCode("chunk_mismatch")
        val INCOMPLETE = ErrorCode("incomplete")
        val UNAUTHORIZED = ErrorCode("unauthorized")
        val DEVICE_REVOKED = ErrorCode("device_revoked")
        val DEVICE_DORMANT = ErrorCode("device_dormant")
        val NOT_FOUND = ErrorCode("not_found")
        val CONTRACT_TOO_OLD = ErrorCode("contract_too_old")
        val HUB_STORAGE_FULL = ErrorCode("hub_storage_full")
        val INTERNAL_ERROR = ErrorCode("internal_error")
        val EPOCH_CHANGED = ErrorCode("epoch_changed")
    }
}
