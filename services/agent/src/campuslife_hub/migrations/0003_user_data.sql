CREATE TABLE tags (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL CHECK (json_valid(data))
);
