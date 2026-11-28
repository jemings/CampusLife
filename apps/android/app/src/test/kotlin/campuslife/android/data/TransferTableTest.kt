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
    fun `only a changed epoch takes a row out of settled, and only it or a changed Hub out of rejected`() {
        for (event in events - OutboxEvent.EPOCH_CHANGED) assertEquals("$event", SETTLED, SETTLED.after(event))
        for (event in events - OutboxEvent.EPOCH_CHANGED - OutboxEvent.HUB_CHANGED) assertEquals("$event", REJECTED, REJECTED.after(event))
    }

    @Test
    fun `a changed Hub sends the rejected to it and moves no other row`() {
        assertEquals(PENDING, REJECTED.after(OutboxEvent.HUB_CHANGED))
        for (state in blobStates + outboxStates - REJECTED) assertEquals("$state", state, state.after(OutboxEvent.HUB_CHANGED))
    }

    @Test
    fun `a changed epoch sends every answered Mutation again and leaves the unsent as they are`() {
        for (state in listOf(ACKED, PULLING, SETTLED, REJECTED)) assertEquals("$state", PENDING, state.after(OutboxEvent.EPOCH_CHANGED))
        for (state in listOf(HELD, PENDING)) assertEquals("$state", state, state.after(OutboxEvent.EPOCH_CHANGED))
    }

    @Test
    fun `a Mutation the Hub cannot read in any batch is set aside with the rejected`() {
        assertEquals(REJECTED, PENDING.after(OutboxEvent.UNSENDABLE))
    }

    @Test
    fun `an answer to a Mutation moves only a pending row`() {
        val answers = listOf(
            OutboxEvent.APPLIED,
            OutboxEvent.CONFLICTED,
            OutboxEvent.PARTLY_APPLIED,
            OutboxEvent.REFUSED,
            OutboxEvent.BLOBS_MISSING,
            OutboxEvent.BLOBS_GONE,
            OutboxEvent.UNSENDABLE,
        )
        for (state in outboxStates - PENDING) for (event in answers) assertEquals("$state $event", state, state.after(event))
    }

    @Test
    fun `a conflict in which the Hub wrote some keys waits for a pull as an applied Mutation does`() {
        assertEquals(PENDING.after(OutboxEvent.APPLIED), PENDING.after(OutboxEvent.PARTLY_APPLIED))
        assertEquals(SETTLED, PENDING.after(OutboxEvent.CONFLICTED))
    }

    @Test
    fun `a held commit whose blob is lost needs attention`() {
        assertEquals(REJECTED, HELD.after(OutboxEvent.BLOB_LOST))
        for (state in outboxStates - HELD) assertEquals("$state", state, state.after(OutboxEvent.BLOB_LOST))
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
