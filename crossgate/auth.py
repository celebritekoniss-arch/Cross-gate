"""Mots de passe (PBKDF2-SHA256) et jetons de session signés (HMAC-SHA256), sans dépendance externe."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(secret: str, username: str, hours: int) -> str:
    payload = _b64(json.dumps({"u": username, "exp": int(time.time() + hours * 3600)}).encode())
    sig = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(secret: str, token: str) -> str | None:
    """Renvoie le nom d'utilisateur si le jeton est valide et non expiré."""
    try:
        payload, sig = token.split(".")
        expected = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_unb64(payload))
        if data["exp"] < time.time():
            return None
        return data["u"]
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
