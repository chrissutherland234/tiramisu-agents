from uuid import UUID

from tiramisu_agents.security.admin_cli import build_parser
from tiramisu_agents.security.tenant_provisioning import (
    LOCAL_DEVELOPMENT_ACTOR_ID,
    LOCAL_DEVELOPMENT_TENANT_ID,
)


def test_admin_cli_parses_tenant_creation_with_an_explicit_id() -> None:
    tenant_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    arguments = build_parser().parse_args(
        [
            "create-tenant",
            "--tenant-id",
            str(tenant_id),
            "--slug",
            "acme-demo",
            "--name",
            "Acme Demo",
        ]
    )

    assert arguments.command == "create-tenant"
    assert arguments.tenant_id == tenant_id
    assert arguments.slug == "acme-demo"
    assert arguments.name == "Acme Demo"


def test_admin_cli_parses_the_deterministic_local_bootstrap_command() -> None:
    arguments = build_parser().parse_args(["bootstrap-local"])

    assert arguments.command == "bootstrap-local"
    assert LOCAL_DEVELOPMENT_TENANT_ID.int == 1
    assert LOCAL_DEVELOPMENT_ACTOR_ID.int == 2
