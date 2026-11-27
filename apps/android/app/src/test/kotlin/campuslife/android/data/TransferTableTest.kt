package campuslife.android.data

import campuslife.android.data.TransferState.ACKED
import campuslife.android.data.TransferState.HELD
import campuslife.android.data.TransferState.PENDING
import campuslife.android.data.TransferState.PULLING
import campuslife.android.data.TransferState.REJECTED
import campuslife.android.data.TransferState.RETRY_WAIT
import campuslife.android.data.TransferState.SETTLED
import campuslife.android.data.TransferState.STORED
import campuslife.android.data.TransferState.UPLOADING
import campuslife.android.data.TransferState.WAITING
import org.junit.Assert.assertEquals
import org.junit.Test

class TransferTableTest {
    private val events: List<TransferEvent> = BlobEvent.entries + OutboxEvent.entries
    private val blobStates = listOf(WAITING, UPLOADING, RETRY_WAIT, STORED)
    private val outboxStates = listOf(HELD, PENDING, ACKED, PULLING, SETTLED, REJECTED)

    @Test
    fun `a blob row moves only on blob events and an outbox row only on outbox events`() {
        for (state in blobStates) for (event in OutboxEvent.entries) assertEquals("$state $event", state, state.after(event))
        for (state in outboxStates) for (event in BlobEvent.entries) assertEquals("$state $event", state, state.after(event))
    }

    @Test
    fun `a blob row stays inside the blob states and an outbox row inside the outbox states`() {
        for (state in blobStates) for (event in events) assertEquals("$state $event", true, state.after(event) in blobStates)
        for (state in outboxStates) for (event in events) assertEquals("$state $event", true, state.after(event) in outboxStates)
    }

    @Test
    fun `a stored blob leaves stored only when the Hub says it lost the blob`() {
        for (event in events) {
            assertEquals("$event", if (event == BlobEvent.HUB_LOST_BLOB) WAITING else STORED, STORED.after(event))
        }
    }

    @Test
    fun `nothing takes a row out of settled or rejected`() {
        for (event in events) {
            assertEquals("$event", SETTLED, SETTLED.after(event))
            assertEquals("$event", REJECTED, REJECTED.after(event))
        }
    }

    @Test
    fun `an answer to a Mutation moves only a pending row`() {
        val answers = listOf(OutboxEvent.APPLIED, OutboxEvent.CONFLICTED, OutboxEvent.REFUSED, OutboxEvent.BLOBS_MISSING, OutboxEvent.BLOBS_GONE)
        for (state in outboxStates - PENDING) for (event in answers) assertEquals("$state $event", state, state.after(event))
    }

    @Test
    fun `every state is reached from where rows start`() {
        val reached = mutableSetOf(WAITING, HELD, PENDING)
        var frontier = reached.toList()
        while (frontier.isNotEmpty()) {
            frontier = frontier.flatMap { state -> events.map(state::after) }.filter(reached::add)
        }
        assertEquals(TransferState.entries.toSet(), reached)
    }

    @Test
    fun `each state has its own column text`() {
        for (state in TransferState.entries) assertEquals(state, TransferState.of(state.column))
    }
}
