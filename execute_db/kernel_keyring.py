"""Kernel keyring access via libkeyutils (ctypes) — no external dependencies.

Token files are encrypted with a passphrase composed of the token secret plus
a random *key share* stored only in the kernel's per-user keyring with a TTL.
The kernel destroys the share at expiry (or on reboot), after which no copy of
the token file can ever be decrypted — the self-destruct lives in the key
material itself, not in a scheduled file deletion.

Linux-only; callers must treat an unavailable keyring as "no share".
"""

import ctypes
import ctypes.util

KEY_SPEC_THREAD_KEYRING = -1  # always possessed by the caller
KEY_SPEC_USER_KEYRING = -4    # @u: shared by all processes of this UID
KEY_POS_ALL = 0x3F000000
KEY_USR_ALL = 0x003F0000
_KEY_TYPE = b"user"

_lib = None
_lib_tried = False


def _keyutils():
    global _lib, _lib_tried
    if _lib_tried:
        return _lib
    _lib_tried = True
    try:
        name = ctypes.util.find_library("keyutils") or "libkeyutils.so.1"
        lib = ctypes.CDLL(name, use_errno=True)
        lib.add_key.restype = ctypes.c_int32
        lib.add_key.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int32]
        lib.keyctl_search.restype = ctypes.c_long
        lib.keyctl_search.argtypes = [ctypes.c_int32, ctypes.c_char_p,
                                      ctypes.c_char_p, ctypes.c_int32]
        lib.keyctl_set_timeout.restype = ctypes.c_long
        lib.keyctl_set_timeout.argtypes = [ctypes.c_int32, ctypes.c_uint]
        lib.keyctl_read.restype = ctypes.c_long
        lib.keyctl_read.argtypes = [ctypes.c_int32, ctypes.c_char_p, ctypes.c_size_t]
        lib.keyctl_revoke.restype = ctypes.c_long
        lib.keyctl_revoke.argtypes = [ctypes.c_int32]
        lib.keyctl_setperm.restype = ctypes.c_long
        lib.keyctl_setperm.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        lib.keyctl_link.restype = ctypes.c_long
        lib.keyctl_link.argtypes = [ctypes.c_int32, ctypes.c_int32]
        lib.keyctl_unlink.restype = ctypes.c_long
        lib.keyctl_unlink.argtypes = [ctypes.c_int32, ctypes.c_int32]
        _lib = lib
    except OSError:
        _lib = None
    return _lib


def available() -> bool:
    return _keyutils() is not None


def store(desc: str, data: bytes, ttl_seconds: int) -> bool:
    """Add a key to the user keyring with a kernel-enforced TTL.

    The key is created in the thread keyring first — a fresh key grants full
    rights only to its possessor, so timeout/permissions must be set while we
    still possess it — then linked into @u for other same-user processes.
    """
    lib = _keyutils()
    if lib is None:
        return False
    key = lib.add_key(_KEY_TYPE, desc.encode(), data, len(data), KEY_SPEC_THREAD_KEYRING)
    if key < 0:
        return False
    ok = (
        lib.keyctl_set_timeout(key, ttl_seconds) >= 0
        and lib.keyctl_setperm(key, KEY_POS_ALL | KEY_USR_ALL) >= 0
        and lib.keyctl_link(key, KEY_SPEC_USER_KEYRING) >= 0
    )
    lib.keyctl_unlink(key, KEY_SPEC_THREAD_KEYRING)
    if not ok:
        lib.keyctl_revoke(key)  # never leave an immortal share behind
        return False
    return True


def read(desc: str):
    """Return the key's payload, or None if missing/expired/unavailable."""
    lib = _keyutils()
    if lib is None:
        return None
    key = lib.keyctl_search(KEY_SPEC_USER_KEYRING, _KEY_TYPE, desc.encode(), 0)
    if key < 0:
        return None
    buf = ctypes.create_string_buffer(512)
    n = lib.keyctl_read(int(key), buf, len(buf))
    if n < 0 or n > len(buf):
        return None
    return buf.raw[:n]


def remove(desc: str) -> bool:
    """Unlink (destroy) a key; True if it existed and was removed."""
    lib = _keyutils()
    if lib is None:
        return False
    key = lib.keyctl_search(KEY_SPEC_USER_KEYRING, _KEY_TYPE, desc.encode(), 0)
    if key < 0:
        return False
    return lib.keyctl_unlink(int(key), KEY_SPEC_USER_KEYRING) >= 0
