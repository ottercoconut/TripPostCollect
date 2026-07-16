"""Encrypted Xiaohongshu storage snapshots and runtime materialization."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any, Iterator

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from trippostcollect.core.paths import ensure_dir, ensure_parent


MAGIC = b"TPCXHS1\0"
KEY_ENV = "TRIPPOSTCOLLECT_XHS_SNAPSHOT_KEY"
KEYCHAIN_SERVICE = "TripPostCollect.XHS"
KEYCHAIN_ACCOUNT = "snapshot-key"


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
    cookies = []
    for item in state.get("cookies", []):
        if not isinstance(item, dict) or not item.get("name") or not item.get("value"):
            continue
        cookie = {key: value for key, value in item.items() if key in allowed_cookie_keys and value is not None}
        if cookie.get("expires") == -1:
            cookie.pop("expires", None)
        cookies.append(cookie)
    if cookies:
        await context.add_cookies(cookies)
    origins: dict[str, dict[str, str]] = {}
    for item in state.get("origins", []):
        if not isinstance(item, dict) or not item.get("origin"):
            continue
        origins[str(item["origin"])] = {
            str(entry["name"]): str(entry["value"])
            for entry in item.get("localStorage", [])
            if isinstance(entry, dict) and entry.get("name") is not None
        }
    if origins:
        encoded = json.dumps(origins, ensure_ascii=False)
        await context.add_init_script(
            f"""() => {{
                const origins = {encoded};
                const values = origins[location.origin] || {{}};
                for (const [key, value] of Object.entries(values)) localStorage.setItem(key, value);
            }}"""
        )
