"""Trusted deployment CLI for credential and tenant safety control operations."""

import argparse
import asyncio
import json
from datetime import datetime
from uuid import UUID

from tiramisu_agents.api.settings import get_settings
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.security.credential_service import TenantCredentialService
from tiramisu_agents.security.credentials import CredentialScope
from tiramisu_agents.security.tenancy import TenantSafetyService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run trusted Tiramisu tenant control-plane operations"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    issue = commands.add_parser("issue-credential", help="issue and print one bearer token")
    issue.add_argument("--tenant-id", type=UUID, required=True)
    issue.add_argument("--actor-id", type=UUID, required=True)
    issue.add_argument("--name", required=True)
    issue.add_argument(
        "--scope",
        action="append",
        type=CredentialScope,
        choices=list(CredentialScope),
        required=True,
    )
    issue.add_argument("--role", action="append", default=[])
    issue.add_argument(
        "--expires-at",
        type=datetime.fromisoformat,
        help="timezone-aware ISO-8601 timestamp",
    )

    revoke = commands.add_parser("revoke-credential", help="revoke a bearer credential")
    revoke.add_argument("--tenant-id", type=UUID, required=True)
    revoke.add_argument("--credential-id", type=UUID, required=True)
    revoke.add_argument("--actor-id", type=UUID, required=True)

    tenant_status = commands.add_parser(
        "set-tenant-status", help="suspend or resume all tenant execution"
    )
    tenant_status.add_argument("--tenant-id", type=UUID, required=True)
    tenant_status.add_argument("--actor-id", type=UUID, required=True)
    tenant_status.add_argument("--status", choices=("active", "suspended"), required=True)
    tenant_status.add_argument("--reason", required=True)
    return parser


async def _execute(arguments: argparse.Namespace) -> dict[str, object]:
    settings = get_settings()
    engine = create_engine(settings.migration_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory.begin() as session:
            if arguments.command == "issue-credential":
                issued = await TenantCredentialService().issue(
                    session,
                    tenant_id=arguments.tenant_id,
                    actor_id=arguments.actor_id,
                    name=arguments.name,
                    scopes=tuple(arguments.scope),
                    roles=tuple(arguments.role),
                    expires_at=arguments.expires_at,
                )
                return {
                    "tenant_id": str(issued.tenant_id),
                    "credential_id": str(issued.credential_id),
                    "actor_id": str(issued.actor_id),
                    "token": issued.token,
                }
            if arguments.command == "revoke-credential":
                revoked = await TenantCredentialService().revoke(
                    session,
                    tenant_id=arguments.tenant_id,
                    credential_id=arguments.credential_id,
                    actor_id=arguments.actor_id,
                )
                return {
                    "credential_id": str(revoked.credential_id),
                    "revoked_at": revoked.revoked_at.isoformat(),
                }
            transition = await TenantSafetyService().set_status(
                session,
                tenant_id=arguments.tenant_id,
                actor_id=arguments.actor_id,
                new_status=arguments.status,
                reason=arguments.reason,
                metadata={"source": "tiramisu-admin"},
            )
            return {
                "event_id": str(transition.event_id),
                "previous_status": transition.previous_status,
                "new_status": transition.new_status,
            }
    finally:
        await engine.dispose()


def run() -> None:
    parser = _parser()
    arguments = parser.parse_args()
    try:
        result = asyncio.run(_execute(arguments))
    except (LookupError, PermissionError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, sort_keys=True))
