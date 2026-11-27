package campuslife.android.data

import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ReferenceDdlTest {
    @Test
    fun `Room creates the tables, keys and indexes of the reference DDL`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val room = Room.inMemoryDatabaseBuilder(context, CampusLifeDatabase::class.java).allowMainThreadQueries().build()
        val reference = SQLiteDatabase.create(null)
        try {
            referenceStatements().forEach(reference::execSQL)
            val created = room.openHelper.writableDatabase

            assertEquals(shapeOf { reference.rawQuery(it, null) }, shapeOf { created.query(it) })
        } finally {
            room.close()
            reference.close()
        }
    }

    private data class Column(
        val name: String,
        val type: String,
        val notNull: Boolean,
        val default: String?,
        val primaryKeyPosition: Int,
        val hidden: Int,
    )

    private data class IndexColumn(val name: String?, val descending: Boolean, val collation: String?, val key: Boolean)

    private data class Index(
        val name: String,
        val unique: Boolean,
        val origin: String,
        val partial: Boolean,
        val columns: List<IndexColumn>,
    )

    private data class ForeignKey(
        val table: String,
        val from: String,
        val to: String,
        val onUpdate: String,
        val onDelete: String,
    )

    private data class Table(
        val columns: List<Column>,
        val autoincrement: Boolean,
        val indexes: Set<Index>,
        val foreignKeys: Set<ForeignKey>,
    )

    private fun shapeOf(query: (String) -> Cursor): Map<String, Table> {
        fun rows(sql: String): List<List<String?>> = query(sql).use { cursor ->
            buildList {
                while (cursor.moveToNext()) {
                    add((0 until cursor.columnCount).map { if (cursor.isNull(it)) null else cursor.getString(it) })
                }
            }
        }

        fun index(row: List<String?>): Index {
            val name = row[1]!!
            val columns = rows("PRAGMA index_xinfo(`$name`)").sortedBy { it[0]!!.toInt() }
                .map { IndexColumn(it[2], it[3] == "1", it[4], it[5] == "1") }
            return Index(name, row[2] == "1", row[3]!!, row[4] == "1", columns)
        }

        val tables = rows(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' " +
                "AND name NOT IN ('sqlite_sequence', 'android_metadata', 'room_master_table')",
        )
        return tables.associate { (name, sql) ->
            name!! to Table(
                columns = rows("PRAGMA table_xinfo(`$name`)")
                    .map { Column(it[1]!!, it[2]!!, it[3] == "1", it[4], it[5]!!.toInt(), it[6]!!.toInt()) },
                autoincrement = sql!!.contains("AUTOINCREMENT", ignoreCase = true),
                indexes = rows("PRAGMA index_list(`$name`)").map(::index).toSet(),
                foreignKeys = rows("PRAGMA foreign_key_list(`$name`)").map { ForeignKey(it[2]!!, it[3]!!, it[4]!!, it[5]!!, it[6]!!) }.toSet(),
            )
        }
    }

    private fun referenceStatements(): List<String> {
        val path = checkNotNull(System.getProperty("campuslife.contract.ddl")) { "campuslife.contract.ddl is not set" }
        return File(path).readLines()
            .joinToString("\n") { it.substringBefore("--") }
            .split(';')
            .map(String::trim)
            .filter(String::isNotEmpty)
    }
}
