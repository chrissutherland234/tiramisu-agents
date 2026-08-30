"""Authentication and live-safety primitives outside model-controlled behavior."""

from tiramisu_agents.security.credentials import (
    CredentialScope,
    IssuedCredential,
    ParsedCredential,
    credential_secret_matches,
    issue_credential,
    parse_credential,
)

__all__ = [
    "CredentialScope",
    "IssuedCredential",
    "ParsedCredential",
    "credential_secret_matches",
    "issue_credential",
    "parse_credential",
]
