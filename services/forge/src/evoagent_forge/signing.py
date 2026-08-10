from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def generate(private_path: Path, public_path: Path) -> str:
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_bytes = public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    public_path.write_bytes(public_bytes)
    return fingerprint(public_bytes)


def fingerprint(public_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(public_bytes).hexdigest()


def sign(artifact: Path, private_path: Path, signature_path: Path) -> dict[str, str]:
    private = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise TypeError("Expected an Ed25519 private key")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    public_bytes = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    payload = {
        "algorithm": "ed25519",
        "digest": f"sha256:{digest}",
        "key_fingerprint": fingerprint(public_bytes),
        "public_key": base64.b64encode(public_bytes).decode(),
        "signature": base64.b64encode(private.sign(bytes.fromhex(digest))).decode(),
    }
    signature_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def verify(artifact: Path, signature_path: Path, trusted_fingerprint: str | None = None) -> bool:
    payload = json.loads(signature_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if payload.get("digest") != f"sha256:{digest}":
        return False
    public_bytes = base64.b64decode(payload["public_key"])
    if trusted_fingerprint and fingerprint(public_bytes) != trusted_fingerprint:
        return False
    try:
        public = serialization.load_pem_public_key(public_bytes)
        if not isinstance(public, Ed25519PublicKey):
            return False
        public.verify(base64.b64decode(payload["signature"]), bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False
    return True
