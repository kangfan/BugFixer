"""Secrets arrive via stdin, environment or Windows user-scoped DPAPI; never argv."""
import ctypes
import contextlib
import getpass
import json
import os
import sqlite3
from pathlib import Path

from .common import BugFixerError, Credential


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise BugFixerError("credential_store_unavailable", "DPAPI credential references require Windows; use stdin or environment credentials.")
    raw = ctypes.create_string_buffer(data)
    src = _Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    dest = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ptr = ctypes.POINTER(_Blob)
    crypt.CryptProtectData.argtypes = [ptr, ctypes.c_wchar_p, ptr, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ptr]
    crypt.CryptUnprotectData.argtypes = [ptr, ctypes.c_void_p, ptr, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ptr]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    # CRYPTPROTECT_UI_FORBIDDEN; DPAPI binds this ciphertext to the current user.
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dest))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(src), "BugFixer", None, None, None, 1, ctypes.byref(dest))
    if not ok:
        raise BugFixerError("credential_store_error", "Windows could not protect/unprotect the credential for this user.")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        kernel.LocalFree(ctypes.cast(dest.data, ctypes.c_void_p))


def _valid_ref(ref: str):
    import re
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", ref):
        raise BugFixerError("invalid_credential_ref", "Credential reference must be a simple local name.")
    return ref


@contextlib.contextmanager
def connect(database: Path):
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    conn.execute('''CREATE TABLE IF NOT EXISTS web_credentials (
        name TEXT PRIMARY KEY,
        source TEXT NOT NULL CHECK(source IN ('zentao','customer_service')),
        environment TEXT NOT NULL DEFAULT 'production' CHECK(environment IN ('production','test')),
        login_url TEXT NOT NULL,
        account TEXT NOT NULL,
        password_encrypted BLOB NOT NULL,
        protection TEXT NOT NULL DEFAULT 'Windows DPAPI current user',
        updated_at TEXT NOT NULL
    )''')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def save(database: Path, ref: str, source: str, environment: str, login_url: str, account: str, password: str):
    from datetime import datetime, timezone
    from .http import validate_url, SERVICE_HOSTS
    _valid_ref(ref)
    validate_url(login_url, SERVICE_HOSTS)
    _validate_scope_url(source, environment, login_url)
    if not account or not password:
        raise BugFixerError("credentials_required", "Account and password are required.")
    encrypted = _dpapi(password.encode('utf-8'))
    with connect(database) as conn:
        conn.execute('''INSERT INTO web_credentials(name,source,environment,login_url,account,password_encrypted,updated_at)
                        VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                        source=excluded.source,environment=excluded.environment,login_url=excluded.login_url,
                        account=excluded.account,password_encrypted=excluded.password_encrypted,updated_at=excluded.updated_at''',
                     (ref, source, environment, login_url, account, encrypted, datetime.now(timezone.utc).isoformat()))
    return {"credential_ref": ref, "protection": "Windows DPAPI current user", "saved": True, "database": str(database.resolve())}


def _validate_scope_url(source, environment, url):
    from urllib.parse import urlsplit
    expected = {('zentao','production'): {'zen.wckj.com'},
                ('customer_service','production'): {'cscmanage.galasports.com','login2.galasports.com'},
                ('customer_service','test'): {'cscmanage.test-galasports.com','login.test-galasports.com'}}
    if urlsplit(url).hostname not in expected.get((source,environment), set()):
        raise BugFixerError('credential_scope_mismatch','Login URL does not belong to the selected system/environment.')


def save_interactive(database: Path, ref: str, source: str, environment: str, login_url: str):
    account = input("Account: ").strip()
    password = getpass.getpass("Password (hidden): ")
    if not account or not password:
        raise BugFixerError("credentials_required", "Account and password are required.")
    return save(database, ref, source, environment, login_url, account, password)


def resolve(request: dict, database: Path) -> Credential:
    data = request.get("credentials") or {}
    ref = request.get("credential_ref")
    if ref:
        _valid_ref(ref)
        if not database.is_file():
            raise BugFixerError("credentials_required", "The named local credential has not been recorded.")
        with contextlib.closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute('SELECT * FROM web_credentials WHERE name=?', (ref,)).fetchone()
            except sqlite3.OperationalError:
                row = None
        if not row:
            raise BugFixerError("credentials_required", "The named local credential has not been recorded.")
        if row['source'] != request['source'] or row['environment'] != request.get('environment', 'production'):
            raise BugFixerError('credential_scope_mismatch', 'Credential source/environment does not match this request.')
        _validate_scope_url(row['source'],row['environment'],row['login_url'])
        data = {'account': row['account'], 'password': _dpapi(row['password_encrypted'], decrypt=True).decode('utf-8'), 'login_url':row['login_url']}
    env = request.get("credential_env") or {}
    return Credential(str(data.get("account") or os.environ.get(env.get("account", ""), "")),
                      str(data.get("password") or os.environ.get(env.get("password", ""), "")),
                      str(data.get("session", "")), str(data.get('login_url','')))
