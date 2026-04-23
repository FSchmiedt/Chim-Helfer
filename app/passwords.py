"""Passwort-Hashing mit stdlib PBKDF2-SHA256.

Bewusst keine externe Abhängigkeit (kein bcrypt/argon2). Für ein Festival-Tool
mit 100-200 Nutzer:innen und gelegentlichem Traffic vollkommen ausreichend.
"""
from __future__ import annotations

import base64
import hashlib
import secrets

ALGO = "pbkdf2_sha256"
ITERATIONS = 240_000  # OWASP 2023 empfohlen für PBKDF2-SHA256
SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Erzeugt einen String der Form `pbkdf2_sha256$iters$salt_b64$hash_b64`."""
    if not password:
        raise ValueError("Passwort darf nicht leer sein.")
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"{ALGO}${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Konstantzeit-Vergleich. `stored=None` gibt stets False zurück."""
    if not stored or not password:
        return False
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        if algo != ALGO:
            return False
        iters = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
        return secrets.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


def generate_token(nbytes: int = 32) -> str:
    """URL-sicherer Zufallstoken für Passwort-Reset-Links."""
    return secrets.token_urlsafe(nbytes)
