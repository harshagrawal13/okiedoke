"""Symmetric encryption for X tokens at rest (Fernet keyed from SECRET_KEY)."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet


def _fernet(secret: str) -> Fernet:
    if not secret:
        raise RuntimeError("SECRET_KEY is required to store X tokens")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt(secret: str, plaintext: str) -> str:
    return _fernet(secret).encrypt(plaintext.encode()).decode()


def decrypt(secret: str, token: str) -> str:
    return _fernet(secret).decrypt(token.encode()).decode()
