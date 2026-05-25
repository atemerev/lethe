use std::sync::Once;

use rusqlite::ffi::{
    sqlite3, sqlite3_api_routines, sqlite3_auto_extension,
};
use sqlite_vec::sqlite3_vec_init;

static REGISTER: Once = Once::new();

type SqliteExtensionInit = unsafe extern "C" fn(
    *mut sqlite3,
    *mut *mut i8,
    *const sqlite3_api_routines,
) -> i32;

pub fn register() {
    REGISTER.call_once(|| unsafe {
        sqlite3_auto_extension(Some(std::mem::transmute::<*const (), SqliteExtensionInit>(
            sqlite3_vec_init as *const (),
        )));
    });
}
