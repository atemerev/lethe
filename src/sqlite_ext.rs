use std::sync::Once;

use rusqlite::ffi::sqlite3_auto_extension;
use sqlite_vec::sqlite3_vec_init;

static REGISTER: Once = Once::new();

pub fn register() {
    REGISTER.call_once(|| unsafe {
        sqlite3_auto_extension(Some(std::mem::transmute(sqlite3_vec_init as *const ())));
    });
}
