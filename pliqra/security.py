from .config import env
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet
from fastapi import HTTPException


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password):
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.urlsafe_b64encode(salt + value).decode()


def verify_password(password, stored):
    try:
        raw = base64.urlsafe_b64decode(stored)
        value = hashlib.scrypt(password.encode(), salt=raw[:16], n=16384, r=8, p=1)
        return hmac.compare_digest(value, raw[16:])
    except (ValueError, TypeError):
        return False


class Vault:
    def __init__(self, directory: Path):
        key = env("PLIQRA_ENCRYPTION_KEY")
        path = directory / "encryption.key"
        if not key:
            if not path.exists():
                database=directory/'pliqra.sqlite3'
                if database.exists():
                    connection=sqlite3.connect(database)
                    try:
                        stored=connection.execute("SELECT secret FROM connections WHERE secret IS NOT NULL AND secret!='' UNION ALL SELECT secret FROM identity_providers WHERE secret!='' LIMIT 1").fetchone()
                        if not stored:
                            stored=connection.execute("SELECT config FROM identity_providers WHERE config LIKE '%sp_private_key%' LIMIT 1").fetchone()
                        if not stored:
                            stored=connection.execute('SELECT payload FROM cache_entries LIMIT 1').fetchone()
                        if not stored:
                            stored=connection.execute('SELECT payload FROM event_content LIMIT 1').fetchone()
                    finally:connection.close()
                    if stored:raise RuntimeError('The encryption key is missing. Restore the original encryption.key or an encrypted backup.')
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(Fernet.generate_key())
            key = path.read_bytes()
        self.fernet = Fernet(key)
        raw_key = key.encode() if isinstance(key, str) else key
        self.key = raw_key
        self.cookie_secret = hmac.new(raw_key, b"pliqra-cookie-signing", hashlib.sha256).hexdigest()
        database=directory/'pliqra.sqlite3'
        if database.exists():
            connection=sqlite3.connect(database)
            try:
                values=[r[0] for r in connection.execute("SELECT secret FROM connections WHERE secret IS NOT NULL AND secret!='' UNION ALL SELECT secret FROM identity_providers WHERE secret!=''").fetchall()]
                import json
                for row in connection.execute('SELECT config FROM identity_providers'):
                    candidate=json.loads(row[0]).get('sp_private_key')
                    if candidate:values.append(candidate)
                values.extend(row[0] for row in connection.execute('SELECT payload FROM cache_entries'))
                values.extend(row[0] for row in connection.execute('SELECT payload FROM event_content'))
                for value in values:self.fernet.decrypt(value.encode())
            except Exception as exc:
                raise RuntimeError('The encryption key does not match stored credentials. Restore the original key or encrypted backup.') from exc
            finally:connection.close()

    def encrypt(self, value):
        return self.fernet.encrypt(value.encode()).decode() if value else ""

    def decrypt(self, value):
        return self.fernet.decrypt(value.encode()).decode() if value else ""

    def session_secret(self):
        # Separate signing material from the encryption key, stable across restarts.
        return self.cookie_secret

    def replace_key(self, key):
        self.fernet = Fernet(key)
        self.key = key
        self.cookie_secret = hmac.new(key, b"pliqra-cookie-signing", hashlib.sha256).hexdigest()


def endpoint_url(value):
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(422, "Enter an http:// or https:// endpoint")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise HTTPException(422, "Put credentials in the API key field; endpoint URLs cannot contain credentials, queries or fragments")
    try:
        parts.port
    except ValueError:
        raise HTTPException(422, "Invalid endpoint port")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
