"""Credential encryption: the four payload shapes, and every way to fail.

Every secret in this file starts with ``SENTINEL-``, so one assertion can prove
that nothing the module produces — a blob, a repr, a dump, an error message, a
log line — carries a credential in the clear.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from cryptography.fernet import Fernet

from mcp_gateway.config import ConfigError
from mcp_gateway.crypto import (
    BearerCredential,
    CredentialCipher,
    CredentialInvalid,
    CredentialUnreadable,
    credential_state,
    generate_key,
    has_credential,
    parse_credential,
)

#: One payload per auth type that stores a credential (spec §4).
PAYLOADS: dict[str, dict[str, Any]] = {
    "bearer": {"type": "bearer", "token": "SENTINEL-TOKEN"},
    "api_key": {"type": "api_key", "header": "X-API-Key", "value": "SENTINEL-APIKEY"},
    "basic": {"type": "basic", "username": "gateway", "password": "SENTINEL-PASSWORD"},
    "headers": {
        "type": "headers",
        "headers": {"X-One": "SENTINEL-ONE", "X-Two": "SENTINEL-TWO"},
    },
}

#: Every value that must never appear in anything the module hands back.
SECRETS = (
    "SENTINEL-TOKEN",
    "SENTINEL-APIKEY",
    "SENTINEL-PASSWORD",
    "SENTINEL-ONE",
    "SENTINEL-TWO",
    "SENTINEL-EXTRA",
)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(generate_key())


@pytest.fixture
def other_cipher() -> CredentialCipher:
    """A second gateway, or the same one after keys.json was lost."""
    return CredentialCipher(generate_key())


def revealed(credential: Any) -> dict[str, Any]:
    """What the credential actually holds — the only place secrets are visible."""
    return json.loads(credential.model_dump_json())


def tampered(blob: bytes) -> bytes:
    """Change exactly one byte in the middle of a blob."""
    index = len(blob) // 2
    replacement = b"A" if blob[index : index + 1] != b"A" else b"B"
    return blob[:index] + replacement + blob[index + 1 :]


@pytest.mark.parametrize("auth_type", list(PAYLOADS))
def test_every_payload_shape_round_trips(cipher: CredentialCipher, auth_type: str) -> None:
    payload = PAYLOADS[auth_type]

    stored = cipher.encrypt_json(payload)

    credential = cipher.decrypt_json(stored)
    assert credential.type == auth_type
    assert revealed(credential) == payload


def test_an_already_built_model_can_be_encrypted_too(cipher: CredentialCipher) -> None:
    # The UI will hand over a dict; the refresh path will hand back a model it
    # has just decrypted. Both are credentials, and both go in the same way.
    model = parse_credential(PAYLOADS["bearer"])

    assert revealed(cipher.decrypt_json(cipher.encrypt_json(model))) == PAYLOADS["bearer"]


def test_the_same_credential_encrypts_differently_every_time(cipher: CredentialCipher) -> None:
    # Fernet carries a timestamp and a random IV, so two servers sharing a token
    # cannot be spotted by comparing their blobs.
    payload = PAYLOADS["bearer"]

    assert cipher.encrypt_json(payload) != cipher.encrypt_json(payload)


def test_the_stored_blob_holds_none_of_the_plaintext(cipher: CredentialCipher) -> None:
    for payload in PAYLOADS.values():
        blob = cipher.encrypt_json(payload).decode("ascii")

        for secret in SECRETS:
            assert secret not in blob


def test_a_credential_encrypted_with_another_key_is_unreadable(
    cipher: CredentialCipher, other_cipher: CredentialCipher
) -> None:
    blob = cipher.encrypt_json(PAYLOADS["bearer"])

    with pytest.raises(CredentialUnreadable) as exc:
        other_cipher.decrypt_json(blob, server_id=7)

    # The UI needs to name the row that has to be fixed, and say what to do.
    assert exc.value.server_id == 7
    assert "server 7" in str(exc.value)
    assert "Enter it again" in str(exc.value)


def test_a_single_altered_byte_is_detected(cipher: CredentialCipher) -> None:
    # Fernet authenticates what it encrypts, so an edited blob fails outright
    # rather than decrypting to something subtly different.
    blob = cipher.encrypt_json(PAYLOADS["api_key"])

    with pytest.raises(CredentialUnreadable):
        cipher.decrypt_json(tampered(blob), server_id=3)


def test_a_truncated_blob_is_detected(cipher: CredentialCipher) -> None:
    blob = cipher.encrypt_json(PAYLOADS["api_key"])

    with pytest.raises(CredentialUnreadable):
        cipher.decrypt_json(blob[:-8])


def test_something_that_is_not_a_blob_at_all_is_unreadable(cipher: CredentialCipher) -> None:
    # A column that was written by hand, or by a version that did not encrypt.
    with pytest.raises(CredentialUnreadable):
        cipher.decrypt_json(b'{"type": "bearer", "token": "SENTINEL-TOKEN"}')


def test_a_decryptable_blob_that_is_not_json_is_unreadable() -> None:
    key = generate_key()
    cipher = CredentialCipher(key)
    blob = Fernet(key.encode("ascii")).encrypt(b"not json at all")

    with pytest.raises(CredentialUnreadable) as exc:
        cipher.decrypt_json(blob, server_id=2)

    assert "not JSON" in str(exc.value)


def test_a_payload_shape_this_version_does_not_know_is_unreadable() -> None:
    # What a downgrade looks like: the key is right, the blob is intact, and the
    # credential inside was written by a version that had another auth type.
    key = generate_key()
    cipher = CredentialCipher(key)
    blob = Fernet(key.encode("ascii")).encrypt(b'{"type": "mtls", "cert": "SENTINEL-TOKEN"}')

    with pytest.raises(CredentialUnreadable) as exc:
        cipher.decrypt_json(blob, server_id=4)

    assert exc.value.server_id == 4
    assert "SENTINEL-TOKEN" not in str(exc.value)


def test_an_unknown_field_is_refused_rather_than_silently_dropped(
    cipher: CredentialCipher,
) -> None:
    with pytest.raises(CredentialInvalid):
        cipher.encrypt_json({**PAYLOADS["bearer"], "surprise": "SENTINEL-EXTRA"})


def test_a_missing_field_is_refused(cipher: CredentialCipher) -> None:
    with pytest.raises(CredentialInvalid):
        cipher.encrypt_json({"type": "api_key", "header": "X-API-Key"})


def test_an_empty_header_map_is_refused(cipher: CredentialCipher) -> None:
    # A `headers` credential with nothing in it would authenticate nothing.
    with pytest.raises(CredentialInvalid):
        cipher.encrypt_json({"type": "headers", "headers": {}})


def test_the_rejection_names_the_field_but_never_the_value(cipher: CredentialCipher) -> None:
    with pytest.raises(CredentialInvalid) as exc:
        cipher.encrypt_json({"type": "bearer", "token": ["SENTINEL-TOKEN"]})

    assert "token" in str(exc.value)
    assert "SENTINEL-TOKEN" not in str(exc.value)


def test_secrets_mask_themselves_in_a_repr_and_an_ordinary_dump() -> None:
    credential = parse_credential(PAYLOADS["basic"])

    # The username is not a secret and stays legible; the password does not.
    assert "gateway" in repr(credential)
    assert "SENTINEL-PASSWORD" not in repr(credential)
    assert "SENTINEL-PASSWORD" not in str(credential.model_dump())
    assert credential.password.get_secret_value() == "SENTINEL-PASSWORD"


def test_nothing_the_module_produces_carries_a_plaintext_credential(
    cipher: CredentialCipher,
    other_cipher: CredentialCipher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Every output of every path at once — success, wrong key, damaged blob, and
    # a rejected payload — plus anything logged while they ran.
    seen: list[str] = []

    with caplog.at_level(logging.DEBUG):
        for payload in PAYLOADS.values():
            blob = cipher.encrypt_json(payload)
            credential = cipher.decrypt_json(blob, server_id=1)
            seen += [blob.decode("ascii"), repr(credential), str(credential.model_dump())]

            with pytest.raises(CredentialUnreadable) as unreadable:
                other_cipher.decrypt_json(blob, server_id=1)
            seen.append(str(unreadable.value))

            with pytest.raises(CredentialUnreadable) as damaged:
                cipher.decrypt_json(tampered(blob), server_id=1)
            seen.append(str(damaged.value))

            with pytest.raises(CredentialInvalid) as rejected:
                cipher.encrypt_json({**payload, "surprise": "SENTINEL-EXTRA"})
            seen.append(str(rejected.value))

    haystack = "\n".join([*seen, caplog.text])
    for secret in SECRETS:
        assert secret not in haystack


def test_a_key_that_is_not_a_fernet_key_is_a_configuration_error() -> None:
    with pytest.raises(ConfigError) as exc:
        CredentialCipher("SENTINEL-BAD-KEY")

    assert "security.encryption_key" in str(exc.value)
    # The message says how to make a good key; it never repeats the bad one.
    assert "SENTINEL-BAD-KEY" not in str(exc.value)


def test_a_key_that_is_not_even_ascii_is_a_configuration_error() -> None:
    with pytest.raises(ConfigError):
        CredentialCipher("kéy-with-an-accent")


def test_a_generated_key_is_usable_as_it_stands() -> None:
    # bootstrap writes exactly this string into keys.json.
    key = generate_key()

    assert isinstance(key, str)
    assert CredentialCipher(key).decrypt_json(
        CredentialCipher(key).encrypt_json(PAYLOADS["bearer"])
    ) == BearerCredential(token="SENTINEL-TOKEN")


def test_the_cipher_never_shows_its_key(cipher: CredentialCipher) -> None:
    assert repr(cipher) == "CredentialCipher(<key withheld>)"


def test_whether_a_credential_is_stored_is_answered_without_the_key(
    cipher: CredentialCipher,
) -> None:
    blob = cipher.encrypt_json(PAYLOADS["bearer"])

    assert has_credential(blob) is True
    assert has_credential(None) is False
    assert has_credential(b"") is False

    assert credential_state("none", None) == "none"
    assert credential_state("bearer", blob) == "stored"
    # Says it authenticates, has nothing to authenticate with: a row to fix.
    assert credential_state("bearer", None) == "missing"
    assert credential_state("api_key", b"") == "missing"
