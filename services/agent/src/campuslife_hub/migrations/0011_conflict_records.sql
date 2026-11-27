-- ConflictRecords (D24 decision 1): what one patch submitted and the Hub did not write, one row per Mutation with
-- id = mutation_id, synced as changes rows of entity conflict_record. They take over the values conflict_stash kept.
CREATE TABLE conflict_records (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL CHECK (json_valid(data))
);

-- A patch resent after its result was forgotten stashed its values again (E2); the first copy of each key stays.
DELETE FROM conflict_stash
WHERE rowid NOT IN (SELECT min(rowid) FROM conflict_stash GROUP BY mutation_id, field);

INSERT INTO conflict_records (id, data)
SELECT mutation_id, json_object('entity_ref', entity || ':' || entity_id, 'id', mutation_id, 'set', json_group_object(field, json(change)))
FROM (SELECT mutation_id, entity, entity_id, field, change FROM conflict_stash ORDER BY mutation_id, field)
GROUP BY mutation_id;

-- Each record takes its own rev above change_seq, in the order its first value was stashed (D3).
INSERT INTO sync_index (entity, id, rev, deleted)
SELECT 'conflict_record', mutation_id, (SELECT change_seq FROM hub_state) + row_number() OVER (ORDER BY first_stashed), 0
FROM (SELECT mutation_id, min(rowid) AS first_stashed FROM conflict_stash GROUP BY mutation_id);

UPDATE hub_state SET change_seq = change_seq + (SELECT count(DISTINCT mutation_id) FROM conflict_stash);

DROP TABLE conflict_stash;
