from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tiramisu_agents.api.main import create_app
from tiramisu_agents.api.settings import Settings


def _settings(**values: object) -> Settings:
    return Settings(**cast(Any, {"_env_file": None, **values}))


def _app(*, settings: Settings | None = None) -> FastAPI:
    return create_app(settings=settings or _settings())


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "tiramisu-api",
        "version": "0.1.0",
        "environment": "development",
    }


@pytest.mark.asyncio
async def test_event_ingestion_is_disabled_without_development_opt_in() -> None:
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/events",
            headers={"X-Tiramisu-Tenant-ID": "00000000-0000-0000-0000-000000000001"},
            json={
                "event_type": "enquiry.created",
                "source": "stub.website",
                "source_event_id": "source-1",
                "occurred_at": "2026-08-30T00:00:00Z",
            },
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_naive_event_timestamp_is_request_validation_error() -> None:
    custom_app = create_app(settings=_settings(allow_unsafe_development_tenant_header=True))
    transport = ASGITransport(app=custom_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/events",
            headers={"X-Tiramisu-Tenant-ID": "00000000-0000-0000-0000-000000000001"},
            json={
                "event_type": "enquiry.created",
                "source": "stub.website",
                "source_event_id": "source-1",
                "occurred_at": "2026-08-30T12:00:00",
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_health_uses_constructed_app_settings() -> None:
    custom_app = create_app(settings=_settings(environment="test"))
    transport = ASGITransport(app=custom_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["environment"] == "test"


@pytest.mark.asyncio
async def test_health_reports_the_exact_client_pack_release() -> None:
    tenant_id = uuid4()
    custom_app = create_app(
        settings=_settings(
            load_fictional_example_processes=True,
            openai_model="test-model",
            deployment_id="fictional-test",
            deployment_build_id="health-test",
            deployment_tenant_ids=(tenant_id,),
        )
    )
    release = custom_app.state.deployment_release
    transport = ASGITransport(app=custom_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "tiramisu-api",
        "version": "0.1.0",
        "environment": "development",
        "deployment_id": "fictional-test",
        "deployment_build_id": "health-test",
        "deployment_release_fingerprint": release.release_fingerprint,
        "client_pack_fingerprint": release.client_pack_fingerprint,
        "model_id": "test-model",
        "temporal_task_queue": release.temporal_task_queue,
    }


@pytest.mark.asyncio
async def test_operator_api_is_disabled_without_development_opt_in() -> None:
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/processes",
            headers={
                "X-Tiramisu-Tenant-ID": "00000000-0000-0000-0000-000000000001",
                "X-Tiramisu-Actor-ID": "00000000-0000-0000-0000-000000000002",
            },
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_operator_api_requires_both_unsafe_identity_headers() -> None:
    custom_app = create_app(settings=_settings(allow_unsafe_development_tenant_header=True))
    transport = ASGITransport(app=custom_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/processes",
            headers={"X-Tiramisu-Tenant-ID": "00000000-0000-0000-0000-000000000001"},
        )

    assert response.status_code == 400
