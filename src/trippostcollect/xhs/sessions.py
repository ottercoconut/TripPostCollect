"""Encrypted Xiaohongshu storage snapshots and runtime materialization."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from trippostcollect.core.paths import ensure_dir, ensure_parent


MAGIC = b"TPCXHS1\0"
KEY_ENV = "TRIPPOSTCOLLECT_XHS_SNAPSHOT_KEY"
KEYCHAIN_SERVICE = "TripPostCollect.XHS"
KEYCHAIN_ACCOUNT = "snapshot-key"
REQUIRED_SESSION_COOKIES = frozenset({"a1", "webId", "web_session"})


def _decode_key(value: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("invalid XHS snapshot key encoding") from exc
    if len(key) != 32:
        raise RuntimeError("XHS snapshot key must decode to 32 bytes")
    return key


def load_snapshot_key(*, create: bool = False) -> bytes:
    env_value = os.environ.get(KEY_ENV, "").strip()
    if env_value:
        return _decode_key(env_value)
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0 and result.stdout.strip():
        return _decode_key(result.stdout.strip())
    if not create:
        raise RuntimeError("XHS snapshot key is missing from macOS Keychain")
    key = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(key).decode("ascii")
    saved = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            KEYCHAIN_ACCOUNT,
            "-w",
            encoded,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if saved.returncode != 0:
        raise RuntimeError(f"cannot store XHS snapshot key in macOS Keychain: {saved.stderr.strip()}")
    return key


def encrypt_storage_state(state: dict[str, Any], destination: str | Path, *, account_id: str, key: bytes) -> Path:
    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encrypted = AESGCM(key).encrypt(nonce, plaintext, account_id.encode("utf-8"))
    target = ensure_parent(Path(destination).expanduser())
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_bytes(MAGIC + nonce + encrypted)
    temporary.chmod(0o600)
    temporary.replace(target)
    target.chmod(0o600)
    return target


def decrypt_storage_state(source: str | Path, *, account_id: str, key: bytes) -> dict[str, Any]:
    path = Path(source).expanduser()
    payload = path.read_bytes()
    if not payload.startswith(MAGIC) or len(payload) <= len(MAGIC) + 12:
        raise RuntimeError(f"invalid encrypted XHS storage state: {path}")
    nonce = payload[len(MAGIC) : len(MAGIC) + 12]
    ciphertext = payload[len(MAGIC) + 12 :]
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, account_id.encode("utf-8"))
    value = json.loads(plaintext.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("decrypted XHS storage state is not an object")
    return value


def snapshot_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def prepare_account_storage_state(
    state: dict[str, Any],
    *,
    account_id: str,
    identity_hash: str,
) -> dict[str, Any]:
    prepared = dict(state)
    metadata = dict(prepared.get("trippostcollect") or {})
    metadata.update(
        {
            "schema_version": 2,
            "platform": "xhs",
            "account_id": account_id,
            "identity_hash": identity_hash,
        }
    )
    metadata.setdefault("captured_at", utc_iso())
    prepared["trippostcollect"] = metadata
    return prepared


def storage_state_is_usable(state: dict[str, Any], *, account_id: str | None = None) -> bool:
    cookie_names = {
        str(item.get("name"))
        for item in state.get("cookies", [])
        if isinstance(item, dict) and item.get("name") and item.get("value")
    }
    if not REQUIRED_SESSION_COOKIES.issubset(cookie_names):
        return False
    metadata = state.get("trippostcollect") or {}
    if account_id and metadata.get("account_id") not in {None, "", account_id}:
        return False
    return True


def refresh_encrypted_storage_state(
    runtime_path: str | Path,
    encrypted_path: str | Path,
    *,
    account_id: str,
    identity_hash: str,
    key: bytes,
) -> bool:
    source = Path(runtime_path).expanduser()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid refreshed XHS storage state") from exc
    if not isinstance(value, dict):
        raise RuntimeError("refreshed XHS storage state is not an object")
    prepared = prepare_account_storage_state(
        value,
        account_id=account_id,
        identity_hash=identity_hash,
    )
    if not storage_state_is_usable(prepared, account_id=account_id):
        raise RuntimeError("refreshed XHS storage state is missing required session cookies")
    existing = decrypt_storage_state(
        encrypted_path,
        account_id=account_id,
        key=key,
    )
    if prepared == existing:
        return False
    encrypt_storage_state(
        prepared,
        encrypted_path,
        account_id=account_id,
        key=key,
    )
    return True


@contextlib.contextmanager
def materialized_storage_state(
    encrypted_path: str | Path,
    runtime_dir: str | Path,
    *,
    account_id: str,
    key: bytes,
) -> Iterator[Path]:
    state = decrypt_storage_state(encrypted_path, account_id=account_id, key=key)
    root = ensure_dir(runtime_dir)
    target = root / "storage_state.json"
    target.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    target.chmod(0o600)
    try:
        yield target
    finally:
        try:
            target.unlink()
        except FileNotFoundError:
            pass


async def restore_context_state(context: Any, state: dict[str, Any]) -> None:
    allowed_cookie_keys = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    existing_cookies = {
        (
            str(item.get("name") or ""),
            str(item.get("domain") or ""),
            str(item.get("path") or "/"),
        )
        for item in await context.cookies()
        if isinstance(item, dict)
    }
    cookies = []
    for item in state.get("cookies", []):
        if not isinstance(item, dict) or not item.get("name") or not item.get("value"):
            continue
        cookie = {key: value for key, value in item.items() if key in allowed_cookie_keys and value is not None}
        if cookie.get("expires") == -1:
            cookie.pop("expires", None)
        cookie_key = (
            str(cookie.get("name") or ""),
            str(cookie.get("domain") or ""),
            str(cookie.get("path") or "/"),
        )
        if cookie_key in existing_cookies:
            continue
        cookies.append(cookie)
    if cookies:
        await context.add_cookies(cookies)
    origins: dict[str, dict[str, dict[str, str]]] = {}
    for item in state.get("origins", []):
        if not isinstance(item, dict) or not item.get("origin"):
            continue
        bucket = origins.setdefault(
            str(item["origin"]),
            {"localStorage": {}, "sessionStorage": {}},
        )
        bucket["localStorage"].update(
            {
                str(entry["name"]): str(entry["value"])
                for entry in item.get("localStorage", [])
                if isinstance(entry, dict) and entry.get("name") is not None
            }
        )
    for item in (state.get("trippostcollect") or {}).get("runtime_storage", []):
        if not isinstance(item, dict) or not item.get("origin"):
            continue
        bucket = origins.setdefault(
            str(item["origin"]),
            {"localStorage": {}, "sessionStorage": {}},
        )
        for storage_key in ("localStorage", "sessionStorage"):
            values = item.get(storage_key)
            if isinstance(values, dict):
                bucket[storage_key].update(
                    {str(key): str(value) for key, value in values.items() if value is not None}
                )
    if origins:
        encoded = json.dumps(origins, ensure_ascii=False)
        await context.add_init_script(
            f"""() => {{
                const origins = {encoded};
                const state = origins[location.origin] || {{}};
                for (const [key, value] of Object.entries(state.localStorage || {{}})) {{
                    if (localStorage.getItem(key) === null) localStorage.setItem(key, value);
                }}
                for (const [key, value] of Object.entries(state.sessionStorage || {{}})) {{
                    if (sessionStorage.getItem(key) === null) sessionStorage.setItem(key, value);
                }}
            }}"""
        )


async def capture_context_state(
    context: Any,
    *,
    account_id: str,
    identity_hash: str,
) -> dict[str, Any]:
    state = await context.storage_state()
    runtime_storage: list[dict[str, Any]] = []
    for page in [page for page in context.pages if not page.is_closed()]:
        url = str(page.url or "")
        if "xiaohongshu.com" not in url and "rednote.com" not in url:
            continue
        try:
            storage = await page.evaluate(
                """
                () => ({
                  origin: location.origin,
                  url: location.href,
                  localStorage: Object.fromEntries(Object.entries(window.localStorage || {})),
                  sessionStorage: Object.fromEntries(Object.entries(window.sessionStorage || {}))
                })
                """
            )
        except Exception as exc:
            storage = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
        if isinstance(storage, dict):
            runtime_storage.append(storage)
    metadata = dict(state.get("trippostcollect") or {})
    metadata.update(
        {
            "schema_version": 2,
            "platform": "xhs",
            "account_id": account_id,
            "identity_hash": identity_hash,
            "captured_at": utc_iso(),
            "runtime_storage": runtime_storage,
        }
    )
    state["trippostcollect"] = metadata
    return state
