"""Encrypted secrets for ``${secret:NAME}`` references, protected with Windows DPAPI.

Values are encrypted for the current Windows user on this machine: nobody else (and no other
PC) can decrypt them. Only names are stored in the clear. The UI never receives a value; it can
only list names, set, replace and delete.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
REFERENCE = re.compile(r"\$\{secret:([A-Za-z_][A-Za-z0-9_]*)\}")
MASK = "***"
MIN_MASKED_LENGTH = 3  # shorter values would blank out ordinary text
_ENTROPY = b"onprem-runner/secrets/v1"


class SecretError(Exception):
    """A secret is missing, invalid or cannot be encrypted."""


class Cipher(Protocol):
    def encrypt(self, data: bytes) -> bytes: ...
    def decrypt(self, data: bytes) -> bytes: ...


class DpapiCipher:
    """Windows Data Protection API, bound to the current user."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise SecretError("encrypted secrets need Windows (DPAPI); not available on this OS")

    def encrypt(self, data: bytes) -> bytes:
        return _dpapi(data, protect=True)

    def decrypt(self, data: bytes) -> bytes:
        return _dpapi(data, protect=False)


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def make_blob(raw: bytes) -> tuple[Blob, Any]:
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    source, keep_source = make_blob(data)
    entropy, keep_entropy = make_blob(_ENTROPY)
    result = Blob()
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args: list[Any] = [ctypes.byref(source)]
    # (data, description in/out: unused, entropy, reserved, prompt: none, flags: 0x1 = no UI, out)
    args += [None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(result)]
    if not function(*args):
        raise SecretError(f"Windows DPAPI failed: {ctypes.WinError(ctypes.get_last_error())}")
    del keep_source, keep_entropy
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


class SecretStore:
    def __init__(self, path: Path, cipher: Cipher | None = None) -> None:
        self.path = path
        self.cipher: Cipher = cipher or DpapiCipher()
        self._lock = threading.Lock()

    # -- management (what the UI may do) ------------------------------------------------
    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._load())

    def exists(self, name: str) -> bool:
        return name in self.names()

    def set(self, name: str, value: str) -> None:
        if not NAME_PATTERN.match(name):
            raise SecretError(
                "name must be letters, digits and underscore, not starting with a digit"
            )
        if not value:
            raise SecretError("a secret cannot be empty")
        encrypted = base64.b64encode(self.cipher.encrypt(value.encode("utf-8"))).decode("ascii")
        with self._lock:
            data = self._load()
            data[name] = encrypted
            self._save(data)

    def delete(self, name: str) -> bool:
        with self._lock:
            data = self._load()
            if name not in data:
                return False
            del data[name]
            self._save(data)
            return True

    # -- use at run time -------------------------------------------------------------------
    def get(self, name: str) -> str:
        with self._lock:
            encrypted = self._load().get(name)
        if encrypted is None:
            raise SecretError(f"secret {name!r} is not set")
        try:
            return self.cipher.decrypt(base64.b64decode(encrypted)).decode("utf-8")
        except SecretError:
            raise
        except Exception as exc:
            raise SecretError(
                f"secret {name!r} cannot be decrypted (was it created by another Windows user "
                f"or on another PC?): {exc}"
            ) from exc

    def expand(self, text: str) -> str:
        """Replace every ``${secret:NAME}`` in ``text`` with its value."""
        return REFERENCE.sub(lambda match: self.get(match.group(1)), text)

    def redactor(self) -> Callable[[str], str]:
        """A function that masks every stored secret value in a piece of text."""
        values = []
        for name in self.names():
            try:
                value = self.get(name)
            except SecretError:
                continue
            if len(value) >= MIN_MASKED_LENGTH:
                values.append(value)
        values.sort(key=len, reverse=True)  # mask the longest first

        def redact(text: str) -> str:
            for value in values:
                if value in text:
                    text = text.replace(value, MASK)
            return text

        return redact

    # -- file ---------------------------------------------------------------------------------
    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            secrets = data["secrets"]
        except (OSError, ValueError, KeyError) as exc:
            raise SecretError(f"secrets file {self.path} is unreadable: {exc}") from exc
        return {str(k): str(v) for k, v in secrets.items()}

    def _save(self, secrets: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "secrets": secrets}, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def referenced_secrets(value: Any) -> set[str]:
    """Names of every ``${secret:NAME}`` inside a nested structure of strings."""
    if isinstance(value, str):
        return set(REFERENCE.findall(value))
    if isinstance(value, list):
        return set().union(*(referenced_secrets(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(referenced_secrets(v) for v in value.values())) if value else set()
    return set()
