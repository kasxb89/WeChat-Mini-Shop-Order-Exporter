"""Identify the loaded runtime independently of the WeChat desktop version."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from functools import lru_cache
import hashlib
import json
from pathlib import Path

PROFILES = json.loads((Path(__file__).parent / 'assets/runtime-profiles.json').read_text(encoding='utf-8'))


def file_version(path):
    version = C.WinDLL('version', use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [W.LPCWSTR, C.POINTER(W.DWORD)]
    version.GetFileVersionInfoW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p]
    version.VerQueryValueW.argtypes = [C.c_void_p, W.LPCWSTR, C.POINTER(C.c_void_p), C.POINTER(W.UINT)]
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ''
    data = C.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, data):
        return ''
    pointer, length = C.c_void_p(), W.UINT()
    if not version.VerQueryValueW(data, '\\', C.byref(pointer), C.byref(length)) or length.value < 52:
        return ''
    values = C.cast(pointer, C.POINTER(W.DWORD))
    return '.'.join(str(value) for value in (values[2] >> 16, values[2] & 65535, values[3] >> 16, values[3] & 65535))


def runtime_library(process):
    # New launchers may live outside the runtime named by --flue-runtime-dir.
    argv = process.cmdline()
    for index, argument in enumerate(argv):
        if argument.startswith('--flue-runtime-dir='):
            candidate = Path(argument.partition('=')[2].strip('"')) / 'flue.dll'
        elif argument == '--flue-runtime-dir' and index + 1 < len(argv):
            candidate = Path(argv[index+1].strip('"')) / 'flue.dll'
        else:
            continue
        if candidate.is_file():
            return candidate
    candidate = Path(process.exe()).parent / 'flue.dll'
    if candidate.is_file():
        return candidate
    return next((Path(item.path) for item in process.memory_maps() if Path(item.path).name.lower() == 'flue.dll'), None)


def match_profile(version, digest):
    for profile in PROFILES:
        if profile.get('sha256') == digest:
            return profile
    number = int(version.rsplit('.', 1)[-1]) if version else None
    return next((p for p in PROFILES if p['version'] == number), None)


@lru_cache(maxsize=8)
def _identify(path, modified, size):
    library = Path(path)
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    version = file_version(library)
    return {'path': str(library), 'version': version, 'sha256': digest,
            'profile': match_profile(version, digest)}


def identify_runtime(library):
    info = library.stat()
    return _identify(str(library), info.st_mtime_ns, info.st_size)
