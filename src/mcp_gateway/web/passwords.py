"""PBKDF2-SHA256 hashing for the one admin password (spec §3.3).

The gateway never stores a password, only a verifier derived from it. An
operator may write the password in plain text in the config file — the file is
theirs and it is the easy way in — but what lives in memory afterwards, and what
they can put in ``admin.password_hash`` instead, is this.

The encoded form is ``pbkdf2_sha256$<iterations>$<salt>$<hash>``: self-describing
on purpose, so a hash written by an older release keeps working when the default
iteration count here is raised. Nothing re-derives a stored hash to a newer cost;
it verifies at the cost it was written with.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Final

ALGORITHM: Final = "pbkdf2_sha256"

#: The cost of a hash derived here. OWASP's floor for PBKDF2-SHA256, and the
#: value spec §3.2 shows in the config example. It is deliberately slow: it is
#: paid once per login and once per startup that derives from ``admin.password``.
DEFAULT_ITERATIONS: Final = 600_000

#: Bytes of salt, rendered as twice as many hex characters.
SALT_BYTES: Final = 16

SEPARATOR: Final = "$"

#: What a malformed hash is measured against, in the message an operator reads.
FORMAT: Final = f"{ALGORITHM}{SEPARATOR}<iterations>{SEPARATOR}<salt>{SEPARATOR}<hash>"


class PasswordHashInvalid(ValueError):  # noqa: N818
    """An encoded hash is not in the format this module writes.

    Named as an adjective because that is the state a caller is reacting to:
    the value in ``admin.password_hash`` is unusable, whoever produced it.
    """


@dataclass(frozen=True)
class PasswordHash:
    """A password verifier: the parameters it was derived with, and the digest."""

    iterations: int
    salt: str
    digest: str

    def __repr__(self) -> str:
        # A digest is not the password, but it is the thing that accepts one,
        # and a settings object holding it gets repr'd in tracebacks and logs.
        return f"{type(self).__name__}(iterations={self.iterations}, digest=<withheld>)"

    def __str__(self) -> str:
        """The encoded form, as ``admin.password_hash`` carries it."""
        return SEPARATOR.join([ALGORITHM, str(self.iterations), self.salt, self.digest])

    def verify(self, password: str) -> bool:
        """Whether ``password`` derives to this digest.

        Constant-time over the digests, which are always the same length, so no
        wrong password is rejected faster than any other.
        """
        return hmac.compare_digest(_derive(password, self.salt, self.iterations), self.digest)


def _derive(password: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    ).hex()


def derive(
    password: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    salt: str | None = None,
) -> PasswordHash:
    """Hash ``password``, with a fresh random salt unless one is given."""
    chosen = secrets.token_hex(SALT_BYTES) if salt is None else salt
    return PasswordHash(iterations, chosen, _derive(password, chosen, iterations))


def parse(encoded: str) -> PasswordHash:
    """Read an encoded hash back into its parts.

    Every rejection names what was wrong with the value rather than only that it
    was wrong: this runs at startup against something an operator typed, and the
    process is about to refuse to start over it.
    """
    parts = encoded.split(SEPARATOR)
    if len(parts) != 4:
        raise PasswordHashInvalid(f"expected {FORMAT}")
    algorithm, iterations, salt, digest = parts
    if algorithm != ALGORITHM:
        raise PasswordHashInvalid(f"unsupported algorithm {algorithm!r}; expected {ALGORITHM!r}")
    if not iterations.isdigit() or int(iterations) < 1:
        raise PasswordHashInvalid(f"iteration count {iterations!r} is not a positive integer")
    if not salt or not digest:
        raise PasswordHashInvalid(f"expected {FORMAT}")
    return PasswordHash(int(iterations), salt, digest)


__all__ = [
    "ALGORITHM",
    "DEFAULT_ITERATIONS",
    "FORMAT",
    "SALT_BYTES",
    "SEPARATOR",
    "PasswordHash",
    "PasswordHashInvalid",
    "derive",
    "parse",
]
