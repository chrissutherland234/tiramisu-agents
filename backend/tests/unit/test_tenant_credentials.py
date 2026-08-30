from uuid import uuid4

import pytest
from tiramisu_agents.security.credentials import (
    credential_secret_matches,
    issue_credential,
    parse_credential,
)


def test_credential_is_tenant_bound_and_verifiable_without_storing_plaintext() -> None:
    tenant_id = uuid4()
    actor_id = uuid4()

    issued = issue_credential(tenant_id, actor_id)
    parsed = parse_credential(issued.token)

    assert parsed.tenant_id == tenant_id
    assert parsed.credential_id == issued.credential_id
    assert issued.actor_id == actor_id
    assert parsed.secret not in issued.secret_hash
    assert credential_secret_matches(parsed.secret, issued.secret_hash)
    assert not credential_secret_matches("A" * 43, issued.secret_hash)


@pytest.mark.parametrize(
    "token",
    (
        "",
        "wrong-prefix.00000000-0000-0000-0000-000000000001.token.secret",
        "tiramisu_v1.not-a-uuid.not-a-uuid.secret",
        "tiramisu_v1.00000000-0000-0000-0000-000000000001."
        "00000000-0000-0000-0000-000000000002.too-short",
        "x" * 513,
    ),
)
def test_malformed_credentials_are_rejected(token: str) -> None:
    with pytest.raises(ValueError, match="credential is malformed"):
        parse_credential(token)
