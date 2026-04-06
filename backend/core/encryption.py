from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

from backend.core.settings import settings

__all__ = ["fer_encrypt", "fer_decrypt"]


class EncryptionSingleton:
    """ONLY use the `_fernet` instance below via the `fer_encrypt` and `fer_decrypt` helper functions."""

    __slots__ = ["_fernet"]

    def __init__(self) -> None:
        self._fernet = self._init_fernet()

    def _init_fernet(self) -> Fernet:
        raw = settings.encryption_key.encode()
        key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        return Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()


_fernet = EncryptionSingleton()


# helper functions to use the singleton instance without exposing it directly
def fer_encrypt(plaintext: str) -> str:
    """Encrypt plaintext using Fernet symmetric encryption."""
    return _fernet.encrypt(plaintext)


def fer_decrypt(ciphertext: str) -> str:
    """Decrypt ciphertext using Fernet symmetric encryption."""
    return _fernet.decrypt(ciphertext)
