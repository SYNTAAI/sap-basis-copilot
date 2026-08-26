"""File-backed login user store for the SyntaAI MCP OAuth server.

Passwords are stored as PBKDF2-HMAC-SHA256 hashes (never plaintext) in
$SYNTAAI_DATA_DIR/users.json. Manage with manage_users.py.
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("SYNTAAI_DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_FILE = DATA_DIR / "users.json"

_ITERATIONS = 200_000
VALID_ROLES = ("admin", "analyst", "viewer")


def _load() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save(users: dict) -> None:
    USERS_FILE.write_text(json.dumps(users, indent=2))
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass


def hash_password(password: str, salt: str | None = None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS)
    return salt, dk.hex()


def add_user(email: str, password: str, name: str = "", role: str = "analyst") -> bool:
    """Create or overwrite a user. Returns True if the email already existed."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("a valid email address is required")
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}")
    users = _load()
    existed = email in users
    salt, h = hash_password(password)
    users[email] = {
        "name": name or email.split("@")[0],
        "role": role,
        "salt": salt,
        "hash": h,
        "created_at": users.get(email, {}).get("created_at", int(time.time())),
        "updated_at": int(time.time()),
    }
    _save(users)
    return existed


def set_password(email: str, password: str) -> None:
    email = (email or "").strip().lower()
    users = _load()
    if email not in users:
        raise KeyError(email)
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt, h = hash_password(password)
    users[email]["salt"] = salt
    users[email]["hash"] = h
    users[email]["updated_at"] = int(time.time())
    _save(users)


def delete_user(email: str) -> bool:
    email = (email or "").strip().lower()
    users = _load()
    if email in users:
        del users[email]
        _save(users)
        return True
    return False


def list_users() -> dict:
    """Return users WITHOUT their salt/hash (safe to print)."""
    return {
        e: {k: v for k, v in d.items() if k not in ("salt", "hash")}
        for e, d in _load().items()
    }


def user_exists(email: str) -> bool:
    """True if the email is a currently-registered user."""
    return (email or "").strip().lower() in _load()


def authenticate(email: str, password: str):
    """Return {email,name,role} on success, else None. Constant-time compare."""
    email = (email or "").strip().lower()
    u = _load().get(email)
    if not u:
        return None
    _, h = hash_password(password or "", u.get("salt", ""))
    if hmac.compare_digest(h, u.get("hash", "")):
        return {"email": email, "name": u.get("name", email), "role": u.get("role", "viewer")}
    return None
