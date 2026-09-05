"""High-entropy, tenant-bound bearer credential creation and verification."""

import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from uuid import UUID, uuid4

_TOKEN_PREFIX = "tiramisu_v1"
_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


class CredentialScope(StrEnum):
    EVENTS_INGEST = "events:ingest"
    QUARANTINE_READ = "quarantine:read"
    QUARANTINE_RESOLVE = "quarantine:resolve"
    PROCESSES_READ = "processes:read"
    REVIEWS_READ = "reviews:read"
    REVIEWS_COMMENT = "reviews:comment"
    REVIEWS_DECIDE = "reviews:decide"
    PROCESSES_CONTROL = "processes:control"
    OUTBOX_READ = "outbox:read"
    OUTBOX_REQUEUE = "outbox:requeue"


@dataclass(frozen=True, slots=True)
class ParsedCredential:
    tenant_id: UUID
    credential_id: UUID
    secret: str


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    tenant_id: UUID
    credential_id: UUID
    actor_id: UUID
    token: str
    secret_hash: str


def issue_credential(
    tenant_id: UUID,
    actor_id: UUID,
    *,
    credential_id: UUID | None = None,
) -> IssuedCredential:
    resolved_id = credential_id or uuid4()
    secret = secrets.token_urlsafe(32)
    return IssuedCredential(
        tenant_id=tenant_id,
        credential_id=resolved_id,
        actor_id=actor_id,
        token=f"{_TOKEN_PREFIX}.{tenant_id}.{resolved_id}.{secret}",
        secret_hash=_hash_secret(secret),
    )


def parse_credential(token: str) -> ParsedCredential:
    if len(token) > 512:
        raise ValueError("credential is malformed")
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _TOKEN_PREFIX:
        raise ValueError("credential is malformed")
    try:
        tenant_id = UUID(parts[1])
        credential_id = UUID(parts[2])
    except ValueError as error:
        raise ValueError("credential is malformed") from error
    secret = parts[3]
    if _SECRET_PATTERN.fullmatch(secret) is None:
        raise ValueError("credential is malformed")
    return ParsedCredential(
        tenant_id=tenant_id,
        credential_id=credential_id,
        secret=secret,
    )


def credential_secret_matches(secret: str, expected_hash: str) -> bool:
    return secrets.compare_digest(_hash_secret(secret), expected_hash)


def _hash_secret(secret: str) -> str:
    return sha256(secret.encode("utf-8")).hexdigest()
