"""Encryption of the credentials the gateway stores for upstream servers.

Spec §1 and §4: an upstream credential lives in SQLite as a Fernet blob, so a
copy of ``gateway.db`` on its own — a backup, a stray file, a disk pulled out of
a laptop — carries nothing usable. The key is ``security.encryption_key``,
generated on first run into ``<data_dir>/keys.json`` (spec §3.2).

Fernet is authenticated encryption, so a blob that has been altered by so much
as one byte fails to decrypt rather than decrypting to something else.

Two things this module is careful about, because both are ways a secret ends up
somewhere it should not be:

*Nothing here renders a credential value.* The payload models hold their secrets
in :class:`~pydantic.SecretStr`, which masks itself in ``repr`` and in ordinary
``model_dump``; the plaintext appears only in the JSON handed to Fernet.
Validation failures are re-raised with the offending *field names* and reasons,
never pydantic's usual echo of the offending input.

*A credential that cannot be read is an expected state, not a crash.* Losing
``keys.json`` and restoring an older one are both things operators do, and both
leave undecryptable rows behind. :class:`CredentialUnreadable` names the server
so the UI can say "credential unreadable — re-enter it" against the right row.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    TypeAdapter,
    ValidationError,
)

from mcp_gateway.config import ConfigError

#: The auth schemes that come with a stored credential (spec §4). ``servers``
#: adds ``none`` to these, which is the one value with no payload shape.
CredentialType = Literal["bearer", "api_key", "basic", "headers"]

#: How a stored credential looks from the outside, without the key: is there one
#: at all, and should there be?
CredentialState = Literal["none", "stored", "missing"]

KEY_ERROR: Final = (
    "security.encryption_key is not a valid Fernet key: it must be 32 url-safe "
    "base64-encoded bytes. Leave it empty to have one generated in keys.json, or "
    "generate one with: "
    "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
)


def generate_key() -> str:
    """A fresh Fernet key, in the form the key file and the config store."""
    return Fernet.generate_key().decode("ascii")


def _reveal(value: SecretStr) -> str:
    return value.get_secret_value()


#: A string that masks itself everywhere except in the JSON that gets encrypted.
#: ``when_used="json"`` is the whole point: ``model_dump()`` still yields the
#: masking :class:`~pydantic.SecretStr`, so only a deliberate serialisation to
#: JSON — which is what :meth:`CredentialCipher.encrypt_json` does — reveals it.
Secret = Annotated[SecretStr, PlainSerializer(_reveal, return_type=str, when_used="json")]


class _CredentialBase(BaseModel):
    """Shared shape rules for every credential payload.

    ``extra="forbid"`` matters more than it looks: it is what stops a payload
    written by a future version, or a hand-edited one, from being accepted with
    half its meaning silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class BearerCredential(_CredentialBase):
    """``Authorization: Bearer <token>``."""

    type: Literal["bearer"] = "bearer"
    token: Secret


class ApiKeyCredential(_CredentialBase):
    """One named header carrying a key, e.g. ``X-API-Key: <value>``."""

    type: Literal["api_key"] = "api_key"
    header: str = Field(min_length=1)
    value: Secret


class BasicCredential(_CredentialBase):
    """HTTP basic auth. The username is not secret; the password is."""

    type: Literal["basic"] = "basic"
    username: str
    password: Secret


class HeadersCredential(_CredentialBase):
    """An arbitrary header map, for upstreams that want more than one."""

    type: Literal["headers"] = "headers"
    headers: dict[str, Secret] = Field(min_length=1)


#: Every payload shape, told apart by ``type`` — the same value the row's
#: ``auth_type`` column carries, so the two can never disagree unnoticed.
Credential = Annotated[
    BearerCredential | ApiKeyCredential | BasicCredential | HeadersCredential,
    Field(discriminator="type"),
]

_ADAPTER: Final[TypeAdapter[Credential]] = TypeAdapter(Credential)


# The two names below are spelled as adjectives on purpose: they read as the
# state a credential is in, which is what a caller catching them is reacting to.
class CredentialInvalid(ValueError):  # noqa: N818
    """A payload does not match any supported credential shape.

    Raised on the way *in*, where it means a caller built the wrong thing.
    """


class CredentialUnreadable(Exception):  # noqa: N818
    """A stored credential could not be decrypted or understood.

    Raised on the way *out*, where it means the row is there but the key is
    wrong, gone, or the blob is damaged. Carries the server so the UI can point
    at the row that needs its credential entered again.
    """

    def __init__(self, *, server_id: int | None = None, reason: str | None = None) -> None:
        self.server_id = server_id
        self.reason = reason
        subject = "a stored credential" if server_id is None else f"server {server_id}"
        detail = f" ({reason})" if reason else ""
        super().__init__(
            f"The credential stored for {subject} cannot be read{detail}. "
            f"Enter it again to replace it."
        )


def _describe(error: ValidationError) -> str:
    """Summarise a validation failure by field, never by value.

    Pydantic's own message quotes the input that failed, which here *is* the
    credential. Only the location and the reason survive.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'payload'}: {item['msg']}"
        for item in error.errors()
    )


def parse_credential(payload: Mapping[str, Any] | Credential) -> Credential:
    """Validate ``payload`` into one of the credential models."""
    if isinstance(payload, _CredentialBase):
        return payload
    try:
        return _ADAPTER.validate_python(payload)
    except ValidationError as exc:
        # from None: the chained traceback would carry pydantic's echo of the
        # input, which is the very thing being kept out of logs.
        raise CredentialInvalid(f"not a usable credential: {_describe(exc)}") from None


def has_credential(blob: bytes | None) -> bool:
    """Whether a credential is stored, answered without needing the key."""
    return bool(blob)


def credential_state(auth_type: str, blob: bytes | None) -> CredentialState:
    """Describe a stored credential without decrypting it.

    ``missing`` is the state worth having a name for: the server says it
    authenticates, but there is no blob to authenticate with — a row half-saved,
    or one whose credential was cleared. The UI shows that as something to fix
    rather than pretending the server is ready to call.
    """
    if auth_type == "none":
        return "none"
    return "stored" if has_credential(blob) else "missing"


class SecretUnreadable(Exception):  # noqa: N818
    """A stored secret that is not a credential could not be decrypted.

    The sibling of :class:`CredentialUnreadable` for the one value that has no
    server to point at (task 125). Its own class rather than that one with
    ``server_id=None``, because the sentence an operator needs is different:
    there is no row to re-enter a credential against, only a setting to set
    again on the page that set it.
    """

    def __init__(self) -> None:
        super().__init__(
            "The stored secret cannot be read: it was encrypted with a different key, "
            "or has been altered. Enter it again to replace it."
        )


class CredentialCipher:
    """Encrypts and decrypts credential payloads with one Fernet key.

    Built once per process from ``security.encryption_key`` and kept on the
    application, so an unusable key is a startup failure rather than something
    the operator discovers when they first try to save a credential.
    """

    __slots__ = ("_fernet",)

    def __init__(self, key: str | bytes) -> None:
        try:
            material = key.encode("ascii") if isinstance(key, str) else key
            self._fernet = Fernet(material)
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            # The key itself is deliberately absent from the message.
            raise ConfigError(KEY_ERROR) from exc

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<key withheld>)"

    def encrypt_json(self, payload: Mapping[str, Any] | Credential) -> bytes:
        """Validate a credential payload and return the blob to store.

        Two calls with the same payload return different blobs: Fernet includes
        a timestamp and a random IV, so equal ciphertexts never give away equal
        credentials.
        """
        credential = parse_credential(payload)
        return self._fernet.encrypt(credential.model_dump_json().encode("utf-8"))

    def decrypt_json(self, blob: bytes, *, server_id: int | None = None) -> Credential:
        """Read a stored blob back into its credential model.

        Every way this can fail — wrong key, damaged blob, a payload shape this
        version does not understand — arrives as :class:`CredentialUnreadable`,
        so a caller has one thing to catch and one thing to tell the operator.
        """
        try:
            plaintext = self._fernet.decrypt(blob)
        except (InvalidToken, TypeError) as exc:
            raise CredentialUnreadable(
                server_id=server_id,
                reason="it was encrypted with a different key, or has been altered",
            ) from exc
        try:
            payload = json.loads(plaintext)
        except json.JSONDecodeError:
            # from None: the decoder's message quotes the document it choked on.
            raise CredentialUnreadable(
                server_id=server_id, reason="the decrypted value is not JSON"
            ) from None
        try:
            return parse_credential(payload)
        except CredentialInvalid as exc:
            raise CredentialUnreadable(server_id=server_id, reason=str(exc)) from None

    def encrypt_text(self, value: str) -> str:
        """Protect one opaque secret that is not an upstream credential.

        The metrics export's licence key (task 125). It has no payload shape to
        validate — it is a string somebody pasted out of another product's UI —
        so it goes through Fernet without passing through the credential models,
        and comes back as text because the row it is stored in is a
        ``settings`` value rather than a blob column. Fernet's output is already
        url-safe base64, so there is nothing to encode on top of it.
        """
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt_text(self, blob: str) -> str:
        """Read one back, or say it cannot be read.

        Every way it can fail — the wrong key, a damaged value, a row edited by
        hand — arrives as :class:`SecretUnreadable`, so a caller has one thing
        to catch and one thing to tell the operator.
        """
        try:
            return self._fernet.decrypt(blob.encode("ascii")).decode("utf-8")
        except (InvalidToken, TypeError, ValueError, UnicodeError) as exc:
            raise SecretUnreadable() from exc
