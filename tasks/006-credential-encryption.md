# Task 006 — Credential encryption

**Milestone:** 2 · Storage
**Depends on:** 005
**Spec:** §3.2, §4

## Goal

Encrypt and decrypt stored credentials so the SQLite file is useless without the key.

## Scope

- `crypto.py` exposing `encrypt_json(dict) -> bytes` and `decrypt_json(bytes) -> dict` over Fernet, keyed from `security.encryption_key`.
- One credential payload shape covering every auth type: `{type, token}`, `{type, header, value}`, `{type, username, password}`, `{type, headers: {...}}`.
- A failed decrypt raises a typed `CredentialUnreadable` error carrying the server id, so callers can surface "credential unreadable — re-enter it" instead of a stack trace.
- Helpers to report whether a credential is set without decrypting it.

## Out of scope

- Key rotation / re-encryption of existing rows.
- OS keyring integration.

## Acceptance

- [x] Round-trip test for all four payload shapes.
- [x] Decrypting with the wrong key raises `CredentialUnreadable`, not a library exception.
- [x] A tampered ciphertext byte is detected and rejected.
- [x] No test or log output ever contains a plaintext credential value.
