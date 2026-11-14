"""Private Session data directories, including macOS extended ACLs."""

import ctypes
import errno
import os
import pathlib
import sys
from functools import lru_cache


@lru_cache
def _libsystem():
    lib = ctypes.CDLL(None, use_errno=True)
    pointer, integer = ctypes.c_void_p, ctypes.c_int
    signatures = {
        "acl_init": (pointer, [integer]), "acl_free": (integer, [pointer]),
        "acl_get_flagset_np": (integer, [pointer, ctypes.POINTER(pointer)]),
        "acl_add_flag_np": (integer, [pointer, integer]),
        "acl_get_fd_np": (pointer, [integer, integer]),
        "acl_get_entry": (integer, [pointer, integer, ctypes.POINTER(pointer)]),
        "acl_get_tag_type": (integer, [pointer, ctypes.POINTER(integer)]),
        "filesec_init": (pointer, []), "filesec_free": (None, [pointer]),
        "filesec_set_property": (integer, [pointer, integer, pointer]),
        "mkdirx_np": (integer, [ctypes.c_char_p, pointer]),
    }
    for name, (result, args) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, args
    return lib


def _os_error():
    number = ctypes.get_errno()
    return OSError(number, os.strerror(number))


def _mkdir_darwin(path: pathlib.Path):
    lib = _libsystem()
    sec = lib.filesec_init()
    acl = ctypes.c_void_p(lib.acl_init(0))
    try:
        flags = ctypes.c_void_p()
        mode = ctypes.c_uint16(0o700)
        # sys/acl.h ACL_FLAG_NO_INHERIT; sys/fcntl.h FILESEC_MODE and FILESEC_ACL.
        if (not sec or not acl or lib.acl_get_flagset_np(acl, ctypes.byref(flags)) or
                lib.acl_add_flag_np(flags, 1 << 17) or lib.filesec_set_property(sec, 4, ctypes.byref(mode)) or
                lib.filesec_set_property(sec, 5, ctypes.byref(acl))):
            raise _os_error()
        if lib.mkdirx_np(os.fsencode(path), sec) and ctypes.get_errno() != errno.EEXIST:
            raise _os_error()
    finally:
        if acl:
            lib.acl_free(acl)
        if sec:
            lib.filesec_free(sec)


def _check_darwin_acl(fd: int):
    lib = _libsystem()
    acl = lib.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            return
        raise _os_error()
    try:
        entry = ctypes.c_void_p()
        cursor = 0  # ACL_FIRST_ENTRY
        while lib.acl_get_entry(acl, cursor, ctypes.byref(entry)) == 0:
            tag = ctypes.c_int()
            if lib.acl_get_tag_type(entry, ctypes.byref(tag)):
                raise _os_error()
            if tag.value != 2:  # ACL_EXTENDED_DENY
                raise PermissionError("the Session data directory grants additional access; select a private directory")
            cursor = -1  # ACL_NEXT_ENTRY
        if ctypes.get_errno() != errno.EINVAL:
            raise _os_error()
    finally:
        lib.acl_free(acl)


def ensure_private_directory(path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        _mkdir_darwin(path)
    else:
        path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink():
        raise PermissionError("the Session data directory must not be a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise PermissionError("the Session data directory must belong to the current user")
        if sys.platform == "darwin":
            _check_darwin_acl(fd)
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
