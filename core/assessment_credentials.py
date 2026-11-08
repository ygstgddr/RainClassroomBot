"""Persist the assessment API key with Windows user-scoped DPAPI."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import tempfile


_KEY_FILENAME = "assessment_api_key.dpapi"
_MAX_KEY_LENGTH = 1024
_MAX_BLOB_BYTES = 16_384
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise OSError("Windows DPAPI is unavailable")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if protect:
        transform = crypt32.CryptProtectData
        description_type = wintypes.LPCWSTR
        output_limit = _MAX_BLOB_BYTES
    else:
        transform = crypt32.CryptUnprotectData
        description_type = ctypes.POINTER(wintypes.LPWSTR)
        output_limit = _MAX_KEY_LENGTH
    transform.argtypes = (
        ctypes.POINTER(_DataBlob), description_type, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    )
    transform.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data, len(data))
    source = _DataBlob(len(data), ctypes.addressof(buffer))
    result = _DataBlob()
    try:
        succeeded = transform(
            ctypes.byref(source), None, None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result),
        )
        if not succeeded or not result.pbData or not 0 < result.cbData <= output_limit:
            raise OSError("Windows DPAPI could not process the credential")
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        if protect:
            ctypes.memset(source.pbData, 0, source.cbData)
        if result.pbData:
            if not protect:
                ctypes.memset(result.pbData, 0, result.cbData)
            local_free(result.pbData)


def _key_bytes(key: str) -> bytes:
    normalized = key.strip() if isinstance(key, str) else ""
    if (not 0 < len(normalized) <= _MAX_KEY_LENGTH
            or any(not "!" <= character <= "~" for character in normalized)):
        raise ValueError("请输入有效的 DeepSeek API 密钥。")
    return normalized.encode("ascii")


def _key_path(root: str | Path) -> Path:
    return Path(root) / "data" / _KEY_FILENAME


def load_key(root: str | Path) -> str:
    """Return the saved key, or an empty string if no key has been saved."""
    try:
        with _key_path(root).open("rb") as credential:
            encrypted = credential.read(_MAX_BLOB_BYTES + 1)
    except FileNotFoundError:
        return ""
    except OSError:
        raise ValueError("无法读取本地保存的 DeepSeek API 密钥。") from None

    try:
        if not 0 < len(encrypted) <= _MAX_BLOB_BYTES:
            raise ValueError
        key = _dpapi(encrypted, protect=False).decode("ascii")
        return _key_bytes(key).decode("ascii")
    except (OSError, ValueError):
        raise ValueError("无法解密本地保存的 DeepSeek API 密钥，请重新保存。") from None


def save_key(root: str | Path, key: str) -> None:
    """Encrypt the key before atomically replacing the stored credential."""
    plaintext = _key_bytes(key)
    path = _key_path(root)
    temporary_path = None
    try:
        encrypted = _dpapi(plaintext, protect=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encrypted)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(path)
        temporary_path = None
    except OSError:
        raise ValueError("无法加密并保存 DeepSeek API 密钥。") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def clear_key(root: str | Path) -> None:
    """Remove the saved credential if it exists."""
    try:
        _key_path(root).unlink(missing_ok=True)
    except OSError:
        raise ValueError("无法清除本地保存的 DeepSeek API 密钥。") from None
