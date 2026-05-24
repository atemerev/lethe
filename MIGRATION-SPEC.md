# LanceDB → SQLite-vec Migration Spec

This document is a **complete, self-contained specification** for a one-shot tool that migrates Lethe's memory data from the old LanceDB-based storage (v0.18.0 and earlier) to the new SQLite-vec-based storage (v0.19.0+). The migrator is expected to live in a separate repository and run on a user's machine against an existing Lethe install. It must not depend on the Lethe binary itself.

## 1. Scope

Lethe historically stored three datasets in LanceDB:

| Dataset          | LanceDB table name | Lethe module   | Source of truth? |
|------------------|--------------------|----------------|------------------|
| Notes            | `notes`            | `src/notes.rs` | No — markdown files in `notes_dir` are authoritative; LanceDB is a derived index. |
| Archival memory  | `archival_memory`  | `src/archival.rs` | **Yes** — LanceDB is the only copy. |
| Message history  | `message_history`  | `src/messages.rs` | **Yes** — LanceDB is the only copy. |

There is also a semantic-search cache (`src/semantic.rs::SemanticIndex`) backed by LanceDB. That cache is purely derived (rebuilt on a fingerprint mismatch) and **does not need to be migrated** — the new build will rebuild it on first use.

**Therefore the migrator must preserve:** all rows of `archival_memory` and `message_history`, including their embedding vectors. Notes can also be migrated, but if migration fails for notes it is recoverable by re-indexing the markdown files on disk.

## 2. Filesystem layout

### 2.1 Old layout (LanceDB)

Given Lethe's `db_path` (`<data>/lethe.db`), the LanceDB root is computed in `src/store.rs::lancedb_dir_for` as:

```
<lancedb_dir> = <data>/memory/lancedb/
```

Inside it, each table is a LanceDB-formatted directory:

```
<lancedb_dir>/
├── notes.lance/              # LanceDB table
├── archival_memory.lance/    # LanceDB table
└── message_history.lance/    # LanceDB table
```

### 2.2 New layout (SQLite-vec)

The new layout collapses all three LanceDB tables into one SQLite file:

```
<data>/memory/lethe-memory.db   # single SQLite file, contains all tables
```

The old `<data>/memory/lancedb/` directory is **left untouched by the migrator** (delete or archive manually after verification).

A separate ephemeral cache for `SemanticIndex` lives at `<data>/memory/semantic-cache.db` and is regenerated automatically; the migrator does not touch it.

## 3. Old LanceDB schemas (read side)

All three tables share these properties:
- `id` (`Utf8`, non-nullable) — primary key
- `vector` (`FixedSizeList<Float32, dim>`, non-nullable) — embedding; `dim` defaults to 768 (`LEGACY_EMBEDDING_DIMENSIONS`)
- All other text/JSON-string fields are `Utf8` non-nullable
- A bootstrap row with `id = "_init_"` was inserted at table creation time and **must be skipped** during migration

### 3.1 `archival_memory.lance`

Arrow schema, field order:

| # | Field        | Arrow type                     | Notes |
|---|--------------|--------------------------------|-------|
| 0 | `id`         | `Utf8`                         | `mem-<uuid-v4>`, or `_init_` for bootstrap |
| 1 | `text`       | `Utf8`                         | User-visible memory text |
| 2 | `vector`     | `FixedSizeList<Float32, dim>`  | Document embedding |
| 3 | `metadata`   | `Utf8`                         | JSON object as string; `"{}"` if empty |
| 4 | `tags`       | `Utf8`                         | JSON array of strings as string; `"[]"` if empty |
| 5 | `created_at` | `Utf8`                         | RFC3339 timestamp |

LanceDB FTS index on `text` column. Index is never queried by Lethe; **migrator does not need to preserve it.**

### 3.2 `message_history.lance`

| # | Field        | Arrow type                    | Notes |
|---|--------------|-------------------------------|-------|
| 0 | `id`         | `Utf8`                        | `msg-<uuid-v4>` or `_init_` |
| 1 | `role`       | `Utf8`                        | `user` / `assistant` / `system` / etc. |
| 2 | `content`    | `Utf8`                        | Message body |
| 3 | `vector`     | `FixedSizeList<Float32, dim>` | Content embedding |
| 4 | `metadata`   | `Utf8`                        | JSON object as string; `"{}"` if empty |
| 5 | `created_at` | `Utf8`                        | RFC3339 timestamp |

LanceDB FTS index on `content`. Not queried; migrator skips.

### 3.3 `notes.lance`

| # | Field         | Arrow type                    | Notes |
|---|---------------|-------------------------------|-------|
| 0 | `id`          | `Utf8`                        | UUID v4 or `_init_` |
| 1 | `title`       | `Utf8`                        | Note title |
| 2 | `text`        | `Utf8`                        | Note body (markdown source) |
| 3 | `tags`        | `Utf8`                        | Comma-separated string (legacy format — **not** JSON) |
| 4 | `file_path`   | `Utf8`                        | Absolute filesystem path of the markdown source |
| 5 | `vector`      | `FixedSizeList<Float32, dim>` | Body embedding |
| 6 | `created_at`  | `Utf8`                        | RFC3339 |
| 7 | `updated_at`  | `Utf8`                        | RFC3339 |

Tag encoding differs from archival/messages — notes store tags as a comma-separated string.

## 4. New SQLite schema (write side)

Open the destination with the `sqlite-vec` extension loaded (see §6). Create all tables in a single transaction.

### 4.1 PRAGMA / setup

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;
```

### 4.2 Unified memory table (archival + notes)

Archival memories and notes now share a single table with a `kind` discriminator. Notes still have their markdown files on disk as the source of truth; the row in `memory` is the indexed/embedded copy.

```sql
CREATE TABLE IF NOT EXISTS memory (
    id          TEXT PRIMARY KEY,             -- "mem-<uuid>" for archival, "note-<uuid>" for notes
    kind        TEXT NOT NULL,                 -- 'archival' | 'note'
    title       TEXT,                          -- present for notes, NULL for archival
    text        TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',   -- JSON object (archival uses this for caller metadata)
    tags        TEXT NOT NULL DEFAULT '[]',   -- JSON array
    file_path   TEXT UNIQUE,                   -- present for notes, NULL for archival
    created_at  TEXT NOT NULL,                 -- RFC3339
    updated_at  TEXT                           -- present for notes
);

CREATE INDEX IF NOT EXISTS memory_kind_idx        ON memory (kind);
CREATE INDEX IF NOT EXISTS memory_created_at_idx  ON memory (created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
    id         TEXT PRIMARY KEY,
    embedding  float[768]
);
```

The migrator must:

- For each `archival_memory.lance` row: insert into `memory` with `kind = 'archival'`, `title = NULL`, `file_path = NULL`, `updated_at = NULL`; preserve `metadata` and `tags` JSON; keep the `mem-<uuid>` id.
- For each `notes.lance` row: insert into `memory` with `kind = 'note'`, `title` from the source title, `file_path` from the source path (must be unique), `metadata = '{}'`; convert the legacy comma-separated `tags` string to a JSON array; preserve `created_at` / `updated_at`.

### 4.3 Message history

Messages remain in their own table — their access patterns (role filtering, tool-call cleanup, bulk recent-N reads) differ enough from archival/notes that sharing the row layout was not worth the join.

```sql
CREATE TABLE IF NOT EXISTS message_history (
    id          TEXT PRIMARY KEY,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS message_history_created_at_idx
    ON message_history (created_at);

CREATE INDEX IF NOT EXISTS message_history_role_idx
    ON message_history (role);

CREATE VIRTUAL TABLE IF NOT EXISTS message_history_vec USING vec0(
    id         TEXT PRIMARY KEY,
    embedding  float[768]
);
```

**Tag format change for notes:** the legacy comma-separated string must be converted to JSON arrays by splitting on `,`, trimming whitespace, filtering empties, and serializing.

## 5. Row mapping

For each old row (skipping `id == "_init_"`):

### 5.1 archival_memory → memory (kind = 'archival')

```
INSERT INTO memory (id, kind, title, text, metadata, tags, file_path, created_at, updated_at)
VALUES (?, 'archival', NULL, ?, ?, ?, NULL, ?, NULL);

INSERT INTO memory_vec (id, embedding)
VALUES (?, ?);
```

- `metadata`: pass through verbatim. If old row has invalid JSON or non-object, replace with `"{}"`.
- `tags`: pass through verbatim. If old row has invalid JSON or non-array, replace with `"[]"`.
- `embedding`: `Vec<f32>` of length 768 (or the dim observed in the source — see §7.1), encoded as raw little-endian f32 bytes (`zerocopy::AsBytes`). If a row has an unexpected dim, **skip the row and log**.

### 5.2 message_history

```
INSERT INTO message_history (id, role, content, metadata, created_at)
VALUES (?, ?, ?, ?, ?);

INSERT INTO message_history_vec (id, embedding)
VALUES (?, ?);
```

Same `metadata` validation rule as archival.

### 5.3 notes → memory (kind = 'note')

```
INSERT INTO memory (id, kind, title, text, metadata, tags, file_path, created_at, updated_at)
VALUES (?, 'note', ?, ?, '{}', ?, ?, ?, ?);

INSERT INTO memory_vec (id, embedding)
VALUES (?, ?);
```

- `tags`: convert from old comma-separated `"foo, bar,baz"` to new JSON array `["foo","bar","baz"]`.
- `file_path`: must be unique. If two source rows collide on `file_path`, abort.

Run the entire migration inside one `BEGIN; ... COMMIT;`. On any error: `ROLLBACK` and bail.

## 6. Reading LanceDB and loading sqlite-vec

### 6.1 Migrator Cargo.toml (minimum)

```toml
[dependencies]
anyhow         = "1"
arrow-array    = "58"
arrow-schema   = "58"
chrono         = "0.4"
futures        = "0.3"
lancedb        = "0.29"
rusqlite       = { version = "0.37", features = ["bundled"] }
serde_json     = "1"
sqlite-vec     = "0.1"
tokio          = { version = "1", features = ["macros", "rt-multi-thread"] }
tracing        = "0.1"
zerocopy       = { version = "0.7", features = ["derive"] }
```

The migrator is the only place these crates need to coexist — once it has run successfully, Lethe itself drops the LanceDB/Arrow stack.

### 6.2 Loading sqlite-vec into rusqlite

Call this **once at process startup** before opening any `Connection`:

```rust
use rusqlite::ffi::sqlite3_auto_extension;
use sqlite_vec::sqlite3_vec_init;

unsafe {
    sqlite3_auto_extension(Some(std::mem::transmute(
        sqlite3_vec_init as *const ()
    )));
}
```

After this, every `Connection::open` will have the `vec0`/`vec_*` functions available.

### 6.3 Encoding vectors

`vec0` expects f32 vectors as raw little-endian bytes:

```rust
use zerocopy::AsBytes;
let v: Vec<f32> = ...;
stmt.execute(rusqlite::params![id, v.as_bytes()])?;
```

### 6.4 Reading LanceDB rows

LanceDB is async; the simplest pattern is a per-table read using a tokio current-thread runtime:

```rust
let db = lancedb::connect(&lancedb_dir.display().to_string())
    .execute().await?;
let table = db.open_table(TABLE_NAME).execute().await?;
let count = table.count_rows(None).await?;
let stream = table.query().limit(count.max(1)).execute().await?;
let batches = stream.try_collect::<Vec<_>>().await?;
```

For each `RecordBatch`, downcast columns to `StringArray` / `FixedSizeListArray<Float32>` and iterate `0..batch.num_rows()`. The current Lethe code in `src/archival.rs::entries_from_batches`, `src/messages.rs::messages_from_batches`, and `src/notes.rs::note_results_from_batches` shows the exact downcast pattern.

For the vector column: get the `FixedSizeListArray`, then for each row index `i` extract the `Float32Array` slice via `list.value(i)` and call `.values().to_vec()` to produce `Vec<f32>`.

## 7. Edge cases & validation

### 7.1 Vector dimension

Default and assumed dim is **768**. The migrator should detect the actual dim from the first non-`_init_` row of each table and:
- if dim == 768 → proceed normally
- if dim != 768 → **abort with an error message** that asks the user to confirm they want to use a non-default embedding model (the sqlite-vec schema embeds dim at create time, so the migrator must use that observed dim when creating `*_vec` tables)

### 7.2 INIT_ID rows

Old code inserted a row with `id = "_init_"` and a zero vector to bootstrap LanceDB schemas (which required at least one row). These rows are explicitly filtered out in every Lethe read path and **must be skipped** by the migrator (do not insert them into SQLite).

### 7.3 Malformed JSON in metadata / tags

If `metadata` does not parse as a JSON object, replace with `"{}"`. If `tags` (archival/messages) does not parse as a JSON array of strings, replace with `"[]"`. Log a warning per offense.

### 7.4 Duplicate IDs

Treat duplicate `id` across rows as a corrupted source — `INSERT` will fail on the PRIMARY KEY constraint and the migrator should abort with the offending id reported. Do not silently skip.

### 7.5 Empty tables

Tables may legitimately exist with only the `_init_` row (empty user data). Migrator should produce empty target tables in that case, not error.

### 7.6 Missing source table

If a `*.lance` directory is missing entirely, treat it as an empty table (this can happen if a user never touched archival memory). Log info; continue.

## 8. Verification

After migration, the migrator must:

1. Compare row counts:
   - `count(memory WHERE kind='archival') == lancedb_rows(archival_memory) - init_rows`
   - `count(memory WHERE kind='note')     == lancedb_rows(notes) - init_rows`
   - `count(message_history)              == lancedb_rows(message_history) - init_rows`
2. Compare vec-table counts: `count(memory_vec) == count(memory)`, and `count(message_history_vec) == count(message_history)`.
3. Sample-check 10 random rows per kind: verify `id`, `text`/`content`, and first 4 dims of the embedding round-trip correctly (read back vector via `vec_to_json` and compare).
4. Exit non-zero on any check failure, with a clear message identifying which kind/table and row.

## 9. CLI surface (suggested)

```
lethe-migrate \
  --lancedb-dir  <data>/memory/lancedb \
  --sqlite-path  <data>/memory/lethe-memory.db \
  [--dry-run]                 # build the destination in a temp path and verify, do not replace
  [--force]                   # overwrite an existing destination file
```

Exit codes:
- `0` — success and verification passed
- `1` — usage / argument error
- `2` — source data missing or unreadable
- `3` — destination already exists and `--force` not given
- `4` — verification failed (destination is left in place for inspection)
- `5` — unexpected error
