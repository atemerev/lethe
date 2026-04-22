"""OS-level write sandbox — Landlock (Linux) / sandbox-exec (macOS).

Restricts the process to only write within allowed directories.
Reads, execution, and network are unrestricted.
Applied once at startup; inherited by all child processes.
"""

import ctypes
import ctypes.util
import logging
import os
import platform
import struct
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Landlock constants (Linux)
# ---------------------------------------------------------------------------

_SYS_landlock_create_ruleset = 444
_SYS_landlock_add_rule = 445
_SYS_landlock_restrict_self = 446

_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

_LANDLOCK_RULE_PATH_BENEATH = 1

# Filesystem access flags
_ACCESS_FS_EXECUTE = 1 << 0
_ACCESS_FS_WRITE_FILE = 1 << 1
_ACCESS_FS_READ_FILE = 1 << 2
_ACCESS_FS_READ_DIR = 1 << 3
_ACCESS_FS_REMOVE_DIR = 1 << 4
_ACCESS_FS_REMOVE_FILE = 1 << 5
_ACCESS_FS_MAKE_CHAR = 1 << 6
_ACCESS_FS_MAKE_DIR = 1 << 7
_ACCESS_FS_MAKE_REG = 1 << 8
_ACCESS_FS_MAKE_SOCK = 1 << 9
_ACCESS_FS_MAKE_FIFO = 1 << 10
_ACCESS_FS_MAKE_BLOCK = 1 << 11
_ACCESS_FS_MAKE_SYM = 1 << 12
_ACCESS_FS_REFER = 1 << 13       # ABI v2
_ACCESS_FS_TRUNCATE = 1 << 14    # ABI v3

# All write-related flags (what we want to restrict globally, then allow per-path)
_WRITE_ACCESS_V1 = (
    _ACCESS_FS_WRITE_FILE
    | _ACCESS_FS_REMOVE_DIR
    | _ACCESS_FS_REMOVE_FILE
    | _ACCESS_FS_MAKE_CHAR
    | _ACCESS_FS_MAKE_DIR
    | _ACCESS_FS_MAKE_REG
    | _ACCESS_FS_MAKE_SOCK
    | _ACCESS_FS_MAKE_FIFO
    | _ACCESS_FS_MAKE_BLOCK
    | _ACCESS_FS_MAKE_SYM
)

_WRITE_ACCESS_V2 = _WRITE_ACCESS_V1 | _ACCESS_FS_REFER
_WRITE_ACCESS_V3 = _WRITE_ACCESS_V2 | _ACCESS_FS_TRUNCATE

PR_SET_NO_NEW_PRIVS = 38


def _landlock_abi_version() -> int:
    """Detect Landlock ABI version. Returns 0 if unsupported."""
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    result = libc.syscall(
        ctypes.c_long(_SYS_landlock_create_ruleset),
        ctypes.c_void_p(0),
        ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    if result < 0:
        return 0
    return result


def _landlock_restrict_writes(writable_paths: list[Path]) -> bool:
    """Apply Landlock write restrictions. Returns True on success."""
    abi = _landlock_abi_version()
    if abi == 0:
        logger.warning("Landlock not supported on this kernel")
        return False

    if abi >= 3:
        write_access = _WRITE_ACCESS_V3
    elif abi >= 2:
        write_access = _WRITE_ACCESS_V2
    else:
        write_access = _WRITE_ACCESS_V1

    # struct landlock_ruleset_attr { u64 handled_access_fs; u64 handled_access_net; u64 scoped; }
    # We only handle filesystem writes — leave net/scoped as 0
    attr = struct.pack("QQQ", write_access, 0, 0)

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

    ruleset_fd = libc.syscall(
        ctypes.c_long(_SYS_landlock_create_ruleset),
        ctypes.c_char_p(attr),
        ctypes.c_size_t(len(attr)),
        ctypes.c_uint32(0),
    )
    if ruleset_fd < 0:
        errno = ctypes.get_errno()
        logger.error("landlock_create_ruleset failed: errno %d", errno)
        return False

    # Add rules for each writable path
    for path in writable_paths:
        resolved = path.resolve()
        resolved.mkdir(parents=True, exist_ok=True)

        fd = os.open(str(resolved), os.O_PATH | os.O_CLOEXEC)
        try:
            # struct landlock_path_beneath_attr { u64 allowed_access; s32 parent_fd; }
            # Pad to 16 bytes (u64 + s32 + 4 bytes padding)
            rule_attr = struct.pack("Qi", write_access, fd)
            result = libc.syscall(
                ctypes.c_long(_SYS_landlock_add_rule),
                ctypes.c_int(ruleset_fd),
                ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                ctypes.c_char_p(rule_attr),
                ctypes.c_uint32(0),
            )
            if result < 0:
                errno = ctypes.get_errno()
                logger.error("landlock_add_rule failed for %s: errno %d", resolved, errno)
                os.close(ruleset_fd)
                return False
            logger.info("Sandbox: write allowed for %s", resolved)
        finally:
            os.close(fd)

    # Must set no_new_privs before restrict_self
    libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)

    result = libc.syscall(
        ctypes.c_long(_SYS_landlock_restrict_self),
        ctypes.c_int(ruleset_fd),
        ctypes.c_uint32(0),
    )
    os.close(ruleset_fd)

    if result < 0:
        errno = ctypes.get_errno()
        logger.error("landlock_restrict_self failed: errno %d", errno)
        return False

    return True


# ---------------------------------------------------------------------------
# macOS sandbox-exec (Seatbelt)
# ---------------------------------------------------------------------------

def _seatbelt_restrict_writes(writable_paths: list[Path]) -> bool:
    """Apply macOS Seatbelt write restrictions via sandbox_init(). Returns True on success."""
    libsandbox = ctypes.util.find_library("sandbox")
    if not libsandbox:
        logger.warning("libsandbox not found on this system")
        return False

    lib = ctypes.CDLL(libsandbox, use_errno=True)

    # Build Seatbelt profile: allow everything, deny writes, then re-allow specific paths
    allow_rules = []
    for path in writable_paths:
        resolved = str(path.resolve())
        resolved.rstrip("/")
        allow_rules.append(f'(allow file-write* (subpath "{resolved}"))')

    # Always allow writes to system temp areas macOS uses internally
    allow_rules.append('(allow file-write* (subpath "/private/tmp"))')
    allow_rules.append('(allow file-write* (subpath "/private/var/folders"))')
    # Allow IPC/mach ports/sysctl (needed for normal process operation)
    allow_rules.append("(allow ipc-posix-shm*)")
    allow_rules.append("(allow mach*)")
    allow_rules.append("(allow sysctl*)")
    allow_rules.append("(allow process-fork)")
    allow_rules.append("(allow signal)")

    profile = (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        + "\n".join(allow_rules)
        + "\n"
    )

    # sandbox_init(const char *profile, uint64_t flags, char **errorbuf)
    # flags=0 means profile is a string (not a named profile)
    lib.sandbox_init.restype = ctypes.c_int
    lib.sandbox_init.argtypes = [
        ctypes.c_char_p,
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.c_char_p),
    ]

    errorbuf = ctypes.c_char_p()
    result = lib.sandbox_init(
        profile.encode("utf-8"),
        0,
        ctypes.byref(errorbuf),
    )

    if result != 0:
        err_msg = errorbuf.value.decode("utf-8", errors="replace") if errorbuf.value else "unknown"
        logger.error("sandbox_init failed: %s", err_msg)
        # Free the error buffer
        lib.sandbox_free_error(errorbuf)
        return False

    for path in writable_paths:
        logger.info("Sandbox: write allowed for %s", path.resolve())

    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_write_sandbox(writable_paths: list[Path]) -> bool:
    """Restrict this process to only write within the given directories.

    Reads, execution, and network are unrestricted.
    Returns True if sandbox was applied, False if unsupported/failed.
    """
    if os.environ.get("LETHE_NO_SANDBOX", "").strip().lower() in ("1", "true", "yes"):
        logger.info("Sandbox disabled via LETHE_NO_SANDBOX")
        return False

    system = platform.system()

    if system == "Linux":
        ok = _landlock_restrict_writes(writable_paths)
        if ok:
            logger.info("Sandbox active (Landlock ABI v%d): writes restricted", _landlock_abi_version())
        return ok

    if system == "Darwin":
        ok = _seatbelt_restrict_writes(writable_paths)
        if ok:
            logger.info("Sandbox active (macOS Seatbelt): writes restricted")
        return ok

    logger.warning("No sandbox available for %s", system)
    return False
