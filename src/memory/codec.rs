use std::path::Path;

use rusqlite::Connection;

use crate::sqlite_ext;

pub fn open_conn(data_path: &Path) -> rusqlite::Result<Connection> {
    sqlite_ext::register();
    let conn = Connection::open(data_path)?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    Ok(conn)
}

pub fn ensure_parent(data_path: &Path) -> std::io::Result<()> {
    if let Some(parent) = data_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    Ok(())
}

pub fn parent_dir(path: &Path) -> &Path {
    path.parent().unwrap_or_else(|| Path::new("."))
}

pub fn f32_slice_as_bytes(values: &[f32]) -> &[u8] {
    unsafe {
        std::slice::from_raw_parts(values.as_ptr() as *const u8, std::mem::size_of_val(values))
    }
}

pub fn semantic_score(distance: f64) -> f64 {
    1.0 / (1.0 + distance.max(0.0))
}
