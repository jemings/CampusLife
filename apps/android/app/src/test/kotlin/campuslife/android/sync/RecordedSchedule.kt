package campuslife.android.sync

/** A [SyncSchedule] that counts what it was asked. */
data class RecordedSchedule(var large: Int = 0, var stops: Int = 0, var syncs: Int = 0, var periodic: Int = 0) : SyncSchedule {
    override fun requestSync() {
        syncs++
    }

    override fun keepPeriodic() {
        periodic++
    }

    override fun requestLarge() {
        large++
    }

    override fun stopUntilUserActs() {
        stops++
    }
}
